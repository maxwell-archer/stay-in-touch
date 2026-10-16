"""Weekday nudge: text Maxwell one name from this week's picks.

Runs from launchd at the configured time. Reads data/picks.json (written by
build.py each Sunday) and data/handles.json, skips anyone Maxwell has
messaged or called since the picks were built, and sends at most one
iMessage per day to his own number via Messages. Never texts anyone else.
"""
import datetime as dt, json, os, shutil, sqlite3, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CFG = json.load(open(os.path.join(ROOT, "config.json")))
APPLE = 978307200
LOG = os.path.join(DATA, "nudge-log.json")


def load(name, default):
    p = os.path.join(DATA, name)
    return json.load(open(p)) if os.path.exists(p) else default


def contacted_since(handles, since):
    """True if Maxwell sent a message to, or had a connected call with, any handle since `since`."""
    if not handles:
        return False
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.expanduser("~/Library/Messages/chat.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(src + ext):
                shutil.copy2(src + ext, os.path.join(tmp, "chat.db" + ext))
        con = sqlite3.connect(f"file:{tmp}/chat.db?mode=ro", uri=True)
        cutoff = (dt.datetime.combine(since, dt.time()).timestamp() - APPLE) * 1e9
        marks = ",".join("?" * len(handles))
        n = con.execute(
            f"""select count(*) from message m join handle h on h.ROWID = m.handle_id
                where m.is_from_me = 1 and m.date > ? and h.id in ({marks})""",
            [cutoff, *handles]).fetchone()[0]
        con.close()
        if n:
            return True
        calls = os.path.join(DATA, "calls.sqlite")
        if os.path.exists(calls):
            c2 = sqlite3.connect(f"file:{calls}?mode=ro", uri=True)
            tables = [r[0] for r in c2.execute("select name from sqlite_master where type='table'")]
            for t in tables:
                cols = [r[1] for r in c2.execute(f"pragma table_info({t})")]
                addr = next((c for c in cols if "address" in c.lower()), None)
                when = next((c for c in cols if "date" in c.lower()), None)
                dur = next((c for c in cols if "duration" in c.lower()), None)
                if addr and when:
                    q = f"select count(*) from {t} where {addr} in ({marks}) and {when} >= ?" + (f" and {dur} > 0" if dur else "")
                    try:
                        if c2.execute(q, [*handles, str(since)]).fetchone()[0]:
                            return True
                    except sqlite3.Error:
                        pass
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def send(text, to):
    script = (
        'on run argv\n'
        '  tell application "Messages"\n'
        '    set svc to 1st account whose service type = iMessage\n'
        '    send (item 1 of argv) to participant (item 2 of argv) of svc\n'
        '  end tell\n'
        'end run')
    subprocess.run(["osascript", "-e", script, text, to], check=True, timeout=60)


def main(dry=False):
    n = CFG["nudge"]
    if not n.get("enabled") and not dry:
        print("nudge disabled in config.json"); return
    today = dt.date.today()
    if n.get("weekdays_only") and today.weekday() >= 5:
        return
    log = load("nudge-log.json", {})
    if log.get("last") == str(today) and not dry:
        return
    picks = load("picks.json", {})
    built = dt.date.fromisoformat(picks.get("built", str(today)))
    handles = load("handles.json", {})
    sent_before = set(log.get("sent", {}).get(picks.get("built", ""), []))
    for p in picks.get("picks", []):
        if p["name"] in sent_before:
            continue
        if contacted_since(handles.get(p["name"], []), built):
            continue
        text = p["nudge"]
        if dry:
            print("would send:", text); return
        send(text, load("local.json", {})["nudge_to"])
        log["last"] = str(today)
        log.setdefault("sent", {}).setdefault(picks["built"], []).append(p["name"])
        json.dump(log, open(LOG, "w"), indent=1)
        print("sent:", text); return
    print("nothing to nudge")


if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
