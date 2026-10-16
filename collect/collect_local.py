#!/usr/bin/env python3
"""Stay-in-Touch local collector (read-only on all source databases).

Copies Messages, WhatsApp, Call History and Contacts databases to a temp dir,
opens the copies read-only, and writes:
  data/history.json  per-person per-day contact counts (last 365 days)
  data/groups.json   last shared small-group message date per person
  data/handles.json  chat.db handle ids + call addresses per person (for nudge checks)
  data/calls.sqlite  accumulating snapshot of connected + unconnected calls
                     (the Mac only keeps ~200 calls per service)

stdlib only. Exit codes: 0 ok, 2 Full Disk Access missing, 3 source missing.
"""
import csv, datetime as dt, glob, json, os, re, shutil, sqlite3, sys, tempfile
from collections import defaultdict

# ---------------------------------------------------------------- config
HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RINGS_CSV = os.path.join(DATA, "rings.csv")

SOURCES = {
    "chat": os.path.join(HOME, "Library/Messages/chat.db"),
    "wa": os.path.join(HOME, "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"),
    "wacontacts": os.path.join(HOME, "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ContactsV2.sqlite"),
    "calls": os.path.join(HOME, "Library/Application Support/CallHistoryDB/CallHistory.storedata"),
}
ADDRESSBOOK_GLOBS = [
    os.path.join(HOME, "Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb"),
    os.path.join(HOME, "Library/Application Support/AddressBook/AddressBook-v22.abcddb"),
]

# Name aliases and exclusions live in data/aliases.json (gitignored: they name people).
#   {"aliases": {"Contacts display name": "rings.csv name"}, "exclude_names": ["service-name", ...]}
_AL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "aliases.json")
_AL = json.load(open(_AL_PATH)) if os.path.exists(_AL_PATH) else {"aliases": {}, "exclude_names": []}
ALIASES = _AL.get("aliases", {})
EXCLUDE_NAME = re.compile("|".join(map(re.escape, _AL.get("exclude_names", []))) or r"(?!x)x", re.I)
WINDOW_DAYS = 365
MAX_GROUP_SIZE = 6          # people in the group including Maxwell
APPLE_EPOCH = 978307200

# ---------------------------------------------------------------- helpers
def die(code, msg):
    print("collect_local: " + msg, file=sys.stderr)
    sys.exit(code)

def check_access():
    p = SOURCES["chat"]
    if not os.path.exists(os.path.dirname(p)):
        die(3, "Messages folder not found: " + p)
    try:
        with open(p, "rb") as f:
            f.read(16)
    except PermissionError:
        die(2, "Full Disk Access missing. Grant it to the app running this script "
               "(System Settings > Privacy & Security > Full Disk Access).")

def copy_db(src, dst_dir, name):
    """Copy a SQLite db and its -wal/-shm sidecars; return path or None."""
    if not os.path.exists(src):
        return None
    dst = os.path.join(dst_dir, name)
    shutil.copy2(src, dst)
    for ext in ("-wal", "-shm"):
        if os.path.exists(src + ext):
            shutil.copy2(src + ext, dst + ext)
    return dst

def ro(path):
    return sqlite3.connect("file:" + path + "?mode=ro", uri=True)

def last10(s):
    return re.sub(r"\D", "", s or "")[-10:]

def norm(s):
    return re.sub(r"[\"“”'‘’]", "", s or "").casefold().strip()

def strip_paren(s):
    return re.sub(r"\s*\(.*?\)\s*$", "", s).strip()

def clean_name(s):
    return re.sub(r"\s+", " ", (s or "").replace("|", " ")).strip()

def local_day(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

# ---------------------------------------------------------------- contacts
class Resolver:
    """Contacts lookup. When one number/email belongs to several contact cards,
    prefer the name that appears in rings.csv, else the first card (by Z_PK,
    Sources/* before the root AddressBook) so results are deterministic."""
    def __init__(self, ab_paths, preferred=()):
        pref = set(preferred)
        cand_phone, cand_email = defaultdict(list), defaultdict(list)
        for p in ab_paths:
            db = ro(p)
            q = ("select r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, x.{col} from {tbl} x "
                 "join ZABCDRECORD r on r.Z_PK = x.ZOWNER order by r.Z_PK, x.Z_PK")
            for tbl, col, cand, keyf in (("ZABCDPHONENUMBER", "ZFULLNUMBER", cand_phone, last10),
                                         ("ZABCDEMAILADDRESS", "ZADDRESS", cand_email, lambda a: (a or "").lower())):
                for fn, ln, org, val in db.execute(q.format(col=col, tbl=tbl)):
                    nm = clean_name(((fn or "") + " " + (ln or "")).strip() or (org or ""))
                    k = keyf(val)
                    if nm and k and nm not in cand[k]:
                        cand[k].append(nm)
            db.close()
        pick = lambda names: next((n for n in names if norm(n) in pref), names[0])
        self.phone = {k: pick(v) for k, v in cand_phone.items()}
        self.email = {k: pick(v) for k, v in cand_email.items()}
        self.by_norm = {}
        for n in list(self.phone.values()) + list(self.email.values()):
            self.by_norm.setdefault(norm(n), n)

    def name(self, handle, display_name=None):
        """Resolve a phone/email handle; unsaved numbers -> '(unsaved) …last4'."""
        if handle:
            if "@" in handle:
                n = self.email.get(handle.lower())
            else:
                n = self.phone.get(last10(handle))
            if n:
                return n
        if display_name and norm(display_name) in self.by_norm:
            return self.by_norm[norm(display_name)]
        digits = re.sub(r"\D", "", handle or "")
        if handle and "@" not in handle and len(digits) >= 10:
            return "(unsaved) …" + digits[-4:]
        return None   # shortcodes, business ids, unresolved emails

# ---------------------------------------------------------------- rings
def load_rings():
    rows = []
    if os.path.exists(RINGS_CSV):
        with open(RINGS_CSV, newline="") as f:
            for r in csv.DictReader(f):
                rows.append(((r.get("name") or "").strip(), (r.get("ring") or "").strip()))
    return rows

def ring_name_map(rings, known_names):
    """Map contact display name -> rings.csv name (same rules as the analysis)."""
    km = {norm(n): n for n in known_names}
    inverse_alias = {v: k for k, v in ALIASES.items()}
    out = {}
    for rname, _ in rings:
        k = inverse_alias.get(rname, rname)
        for cand in (k, strip_paren(k)):
            if norm(cand) in km:
                out.setdefault(km[norm(cand)], rname)
                break
    return out

# ---------------------------------------------------------------- calls snapshot
def snapshot_calls(calls_copy):
    snap_path = os.path.join(DATA, "calls.sqlite")
    snap = sqlite3.connect(snap_path)
    snap.execute("""create table if not exists calls(
        uid text primary key, ts real, address text, duration real,
        originated integer, answered integer, service text, first_seen text)""")
    if calls_copy:
        now = dt.datetime.now().isoformat(timespec="seconds")
        src = ro(calls_copy)
        for uid, date, addr, dur, orig, ans, svc in src.execute(
                "select ZUNIQUE_ID, ZDATE, ZADDRESS, ZDURATION, ZORIGINATED, ZANSWERED, ZSERVICE_PROVIDER from ZCALLRECORD"):
            if isinstance(addr, bytes):
                addr = addr.decode("utf-8", "ignore")
            key = uid or "%s|%s|%s" % (date, addr, dur)
            snap.execute("""insert into calls values(?,?,?,?,?,?,?,?)
                on conflict(uid) do update set ts=excluded.ts, address=excluded.address,
                duration=excluded.duration, originated=excluded.originated,
                answered=excluded.answered, service=excluded.service""",
                (key, (date or 0) + APPLE_EPOCH, addr, dur or 0, orig, ans, svc, now))
        src.close()
        snap.commit()
    rows = snap.execute("select ts, address, duration, originated from calls order by ts").fetchall()
    total = len(rows)
    snap.close()
    return rows, total

# ---------------------------------------------------------------- main
def main():
    check_access()
    os.makedirs(DATA, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="sit-collect-")
    try:
        paths = {k: copy_db(v, tmp, k + ".db") for k, v in SOURCES.items()}
        if not paths["chat"]:
            die(3, "chat.db not found")
        ab = []
        for i, src in enumerate(dict.fromkeys(p for g in ADDRESSBOOK_GLOBS for p in sorted(glob.glob(g)))):
            c = copy_db(src, tmp, "ab%d.abcddb" % i)
            if c:
                ab.append(c)
        rings = load_rings()
        inverse_alias = {v: k for k, v in ALIASES.items()}
        preferred = set()
        for rname, _ in rings:
            k = inverse_alias.get(rname, rname)
            preferred |= {norm(k), norm(strip_paren(k))}
        R = Resolver(ab, preferred)

        today = dt.date.today()
        start = today - dt.timedelta(days=WINDOW_DAYS - 1)
        S, E = start.isoformat(), today.isoformat()

        # per person per day: [text_me, text_them, wa_me, wa_them, calls]
        days = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0]))
        dirs = defaultdict(lambda: [0, 0])     # (from_me, from_them) messages in window
        has_call = set()

        def add(name, ts, slot):
            d = local_day(ts)
            if S <= d <= E:
                days[name][d][slot] += 1
                return True
            return False

        # --- iMessage / SMS / RCS 1:1 (audio messages included; reactions excluded)
        chat = ro(paths["chat"])
        for ident, fm, ts in chat.execute("""
                select c.chat_identifier, m.is_from_me, m.date/1000000000 + ?
                from chat c join chat_message_join j on j.chat_id = c.ROWID
                join message m on m.ROWID = j.message_id
                where c.style = 45 and m.associated_message_type = 0
                order by m.ROWID""", (APPLE_EPOCH,)):
            if not ident or ident.startswith("urn:"):
                continue
            n = R.name(ident)
            if n and add(n, ts, 0 if fm else 1):
                dirs[n][0 if fm else 1] += 1

        # --- WhatsApp 1:1
        if paths["wa"]:
            lid = {}
            if paths["wacontacts"]:
                wc = ro(paths["wacontacts"])
                for l, wid, ph in wc.execute(
                        "select ZLID, ZWHATSAPPID, ZPHONENUMBER from ZWAADDRESSBOOKCONTACT where ZLID is not null"):
                    lid[l] = wid or ph
                wc.close()
            wa = ro(paths["wa"])
            for jid, pn, fm, ts in wa.execute("""
                    select s.ZCONTACTJID, s.ZPARTNERNAME, m.ZISFROMME, m.ZMESSAGEDATE + ?
                    from ZWAMESSAGE m join ZWACHATSESSION s on s.Z_PK = m.ZCHATSESSION
                    where s.ZSESSIONTYPE = 0 and (s.ZCONTACTJID like '%@s.whatsapp.net' or s.ZCONTACTJID like '%@lid')
                    order by m.Z_PK""", (APPLE_EPOCH,)):
                j = lid.get(jid, "") if jid.endswith("@lid") else jid
                n = R.name(j.split("@")[0] if j else None, pn)
                if n and add(n, ts, 2 if fm else 3):
                    dirs[n][0 if fm else 1] += 1
            wa.close()

        # --- calls (accumulated snapshot, connected only)
        call_rows, calls_in_snapshot = snapshot_calls(paths["calls"])
        for ts, addr, dur, _orig in call_rows:
            if (dur or 0) <= 0:
                continue
            n = R.name(addr)
            if n and add(n, ts, 4):
                has_call.add(n)

        # --- exclusions: texting-only people need both directions in window
        keep = {}
        for n, dd in days.items():
            if EXCLUDE_NAME.search(n):
                continue
            if n not in has_call and (dirs[n][0] == 0 or dirs[n][1] == 0):
                continue
            keep[n] = dd

        # --- groups (<= MAX_GROUP_SIZE people incl. Maxwell)
        members = defaultdict(set)
        for cid, h in chat.execute("""select j.chat_id, h.id from chat_handle_join j
                                      join handle h on h.ROWID = j.handle_id"""):
            members[cid].add(h)
        group_last = {}
        for cid, fm, sender, ts in chat.execute("""
                select c.ROWID, m.is_from_me, h.id, m.date/1000000000 + ?
                from chat c join chat_message_join j on j.chat_id = c.ROWID
                join message m on m.ROWID = j.message_id
                left join handle h on h.ROWID = m.handle_id
                where c.style = 43 and m.associated_message_type = 0""", (APPLE_EPOCH,)):
            ms = members.get(cid, ())
            if not ms or len(ms) + 1 > MAX_GROUP_SIZE:
                continue
            for h in ms:
                if fm or (sender and (sender == h if "@" in h else last10(sender) == last10(h))):
                    n = R.name(h)
                    if n and not n.startswith("(unsaved)"):
                        d = local_day(ts)
                        if d > group_last.get(n, ""):
                            group_last[n] = d
        # --- handles per person (chat.db handle.id + call addresses), for nudge checks
        handles = defaultdict(set)
        for (hid,) in chat.execute("select distinct id from handle where id is not null"):
            n = R.name(hid)
            if n:
                handles[n].add(hid)
        for (ident,) in chat.execute("select distinct chat_identifier from chat where style = 45 and chat_identifier is not null"):
            n = R.name(ident)
            if n and not ident.startswith("urn:"):
                handles[n].add(ident)
        for _ts, addr, _dur, _orig in call_rows:
            n = R.name(addr)
            if n and addr:
                handles[n].add(addr)
        chat.close()

        # --- rename to rings.csv names where matched
        rmap = ring_name_map(rings, set(keep) | set(group_last) | set(R.phone.values()) | set(R.email.values()))
        people = {}
        for n in sorted(keep):
            people[rmap.get(n, n)] = {d: keep[n][d] for d in sorted(keep[n])}
        groups = {rmap.get(n, n): group_last[n] for n in sorted(group_last)}

        hist = {"generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "window_start": S, "people": people}
        tmp_out = os.path.join(DATA, "history.json.tmp")
        with open(tmp_out, "w") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1, sort_keys=False)
        os.replace(tmp_out, os.path.join(DATA, "history.json"))
        with open(os.path.join(DATA, "groups.json.tmp"), "w") as f:
            json.dump(groups, f, ensure_ascii=False, indent=1)
        os.replace(os.path.join(DATA, "groups.json.tmp"), os.path.join(DATA, "groups.json"))

        hmap = {rmap.get(n, n): sorted(handles.get(n, ())) for n in sorted(keep)}
        with open(os.path.join(DATA, "handles.json.tmp"), "w") as f:
            json.dump(hmap, f, ensure_ascii=False, indent=1)
        os.replace(os.path.join(DATA, "handles.json.tmp"), os.path.join(DATA, "handles.json"))

        ndays = sum(len(v) for v in people.values())
        print("collect_local ok: %d people, %d person-days (%s..%s), %d calls in snapshot, %d group contacts"
              % (len(people), ndays, S, E, calls_in_snapshot, len(groups)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
