"""Build the Stay in Touch page from local data.

Inputs (all in data/, none committed):
  rings.csv            name,ring  (Maxwell's judgment; ring changes from the app are folded in)
  history.json         per-day texts / WhatsApp / calls, from collect/collect_local.py
  groups.json          last shared small-group-chat date per person
  email-days.json      {"Name": ["YYYY-MM-DD", ...]}  maintained by the weekly task
  calendar-days.json   {"Name": {"dates": [...], "title_inferred": [...]}}
  actions.json         taps saved in the app (ring changes, logged contacts, done, snooze)
Outputs:
  dist/stay-in-touch.html, data/picks.json, data/summary.json, data/picks-history.json

Usage: python3 build.py [--date YYYY-MM-DD]
"""
import csv, datetime as dt, json, os, sys
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CFG = json.load(open(os.path.join(ROOT, "config.json")))
TH = CFG["thresholds_days"]
ORDER = ["Inner", "Close", "Keep", "Wide"]
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
D = lambda s: dt.date.fromisoformat(s[:10]) if s else None
T = D(sys.argv[sys.argv.index("--date") + 1]) if "--date" in sys.argv else dt.date.today()


def load(name, default):
    p = os.path.join(DATA, name)
    return json.load(open(p)) if os.path.exists(p) else default


def label(d):
    return f"{d.day} {MON[d.month - 1]}"


# ---------- inputs ----------
rings = {r["name"]: r["ring"] for r in csv.DictReader(open(os.path.join(DATA, "rings.csv")))}
actions = load("actions.json", {})
acts = {a["name"]: a for a in actions.values() if isinstance(a, dict) and a.get("name")}
for n, a in acts.items():
    if a.get("ring") and n in rings and a["ring"] != rings[n]:
        rings[n] = a["ring"]
hist = load("history.json", {}).get("people", {})
groups = load("groups.json", {})
email = load("email-days.json", {})
cal_raw = load("calendar-days.json", {})
cal = {n: v.get("dates", []) if isinstance(v, dict) else v for n, v in cal_raw.items()
       if "unclear" not in n and not n.startswith("LL (")}
lo365 = T - dt.timedelta(days=364)

# per person day sets: contact (any), mine (my move), theirs (their text/WhatsApp), by channel
names = set(rings) | set(hist) | set(cal) | set(email)
P = {}
for n in names:
    ch = {"text": set(), "whatsapp": set(), "call": set(), "email": set(), "calendar": set(), "logged": set()}
    mine, theirs = set(), {}
    for day, v in hist.get(n, {}).items():
        d = D(day)
        if d > T:
            continue
        tm, tt, wm, wt, ca = (list(v) + [0] * 5)[:5]
        if tm or tt: ch["text"].add(d)
        if wm or wt: ch["whatsapp"].add(d)
        if ca: ch["call"].add(d); mine.add(d)
        if tm or wm: mine.add(d)
        if tt: theirs[d] = "text"
        if wt and d not in theirs: theirs[d] = "whatsapp"
    for x in email.get(n, []):
        d = D(x)
        if d <= T: ch["email"].add(d); mine.add(d)
    for x in cal.get(n, []):
        d = D(x)
        if d <= T: ch["calendar"].add(d); mine.add(d)
    lg = (acts.get(n) or {}).get("logged")
    if lg and lg.get("date") and D(lg["date"]) <= T:
        d = D(lg["date"]); ch["logged"].add(d); mine.add(d)
    contact = set().union(*ch.values())
    P[n] = dict(ch=ch, contact=sorted(contact), mine=sorted(mine), theirs=theirs)


def last_le(arr, w):
    r = None
    for x in arr:
        if x <= w: r = x
        else: break
    return r


def state_at(n, w):
    ring = rings[n]; th = TH[ring]; p = P[n]
    lc = last_le(p["contact"], w)
    days = (w - lc).days if lc else None
    st = "nodata" if days is None else "live" if days <= th else "drifting" if days <= 2 * th else "dormant"
    lm = last_le(p["mine"], w)
    tds = sorted(d for d in p["theirs"] if d <= w)
    lt = tds[-1] if tds else None
    turn = bool(lt and (lm is None or lt > lm) and (w - lt).days <= 60 and (lm is None or (w - lm).days >= 7))
    since_me = (w - lm).days if lm else None
    urgent = (turn and since_me is not None and since_me >= th) or (st == "dormant" and ring in ("Inner", "Close"))
    return dict(state=st, turn=turn, days=days, last=lc, lm=lm, lt=lt, since_me=since_me, urgent=urgent)


ringed = [n for n in rings if rings[n] in TH]

# ---------- DATA (current state per ringed person) ----------
out = []
for n in ringed:
    s = state_at(n, T); p = P[n]
    lastch = {k: max(v) for k, v in p["ch"].items() if v}
    best = max(lastch.items(), key=lambda kv: kv[1])[0] if lastch else None
    seen = None if s["days"] is None else next((k for k in ORDER if s["days"] <= TH[k]), "none")
    g = D(groups.get(n))
    out.append(dict(name=n, ring=rings[n], state=s["state"], turn=s["turn"],
                    turn_channel=p["theirs"][s["lt"]] if s["turn"] else None,
                    turn_date=str(s["lt"]) if s["turn"] else None, days=s["days"], channel=best,
                    last=str(s["last"]) if s["last"] else None, seen=seen, since_me=s["since_me"],
                    group=(T - g).days if g else None,
                    channels={k: str(v) for k, v in lastch.items() if v >= lo365}))

# ---------- weekly trend ----------
ends = [T - dt.timedelta(days=7 * i) for i in range(19, -1, -1)]
weeks = []
for w in ends:
    live, tot_turn, tot_urg = {}, 0, 0
    for r in ORDER:
        ppl = [n for n in ringed if rings[n] == r]; ok = 0
        for n in ppl:
            s = state_at(n, w); ok += s["state"] == "live"; tot_turn += s["turn"]; tot_urg += s["urgent"]
        live[r] = round(ok / len(ppl) * 100) if ppl else 0
    ws = w - dt.timedelta(days=6)
    reached = sum(1 for n in ringed if any(ws <= x <= w for x in P[n]["contact"]))
    weeks.append(dict(end=str(w), label=label(w), live=live, turn=tot_turn, urgent=tot_urg, reached=reached))


def shown_at(n, w):
    s = state_at(n, w); return "turn" if s["turn"] else s["state"]


movers = []
for n in ringed:
    a, b = shown_at(n, ends[-2]), shown_at(n, ends[-1])
    if a != b and "nodata" not in (a, b):
        movers.append({"name": n, "ring": rings[n], "from": a, "to": b})
movers.sort(key=lambda m: (ORDER.index(m["ring"]), m["name"]))

heat = [dict(name=n, ring=r, states=[shown_at(n, w) for w in ends])
        for r in ("Inner", "Close") for n in sorted(x for x in ringed if rings[x] == r)]
gaps = {}
for r in ("Keep", "Wide"):
    ppl = [n for n in ringed if rings[n] == r]; rows = []
    for w in ends:
        c = Counter(shown_at(n, w) for n in ppl)
        rows.append(dict(turn=c["turn"], drifting=c["drifting"], dormant=c["dormant"]))
    gaps[r] = rows


# ---------- chart cards: who moved over the last four weeks ----------
w_then, w_now = ends[-5], ends[-1]


def tag(n, **kw):
    return dict(name=n, ring=rings[n], **kw)


def reached_in(n, w):
    ws = w - dt.timedelta(days=6)
    return any(ws <= x <= w for x in P[n]["contact"])


cards = dict(since=label(w_then))
for r in ("Keep", "Wide"):
    quiet, back = [], []
    for n in (x for x in ringed if rings[x] == r):
        a, b = shown_at(n, w_then), shown_at(n, w_now)
        if "nodata" in (a, b):
            continue
        if a == "live" and b in ("drifting", "dormant"):
            quiet.append(tag(n, to=b, days=state_at(n, w_now)["days"]))
        elif a != "live" and b == "live":
            back.append(tag(n, frm=a))
    quiet.sort(key=lambda x: -(x["days"] or 0)); back.sort(key=lambda x: x["name"])
    cards[r] = dict(quiet=quiet, back=back)
was = {n: state_at(n, w_then)["turn"] for n in ringed}
cards["turn"] = dict(new=sorted((tag(n) for n in ringed if state_at(n, w_now)["turn"] and not was[n]), key=lambda x: x["name"]),
                     cleared=sorted((tag(n) for n in ringed if was[n] and not state_at(n, w_now)["turn"]), key=lambda x: x["name"]))
now_set = {n for n in ringed if reached_in(n, w_now)}
prev = {n: sum(reached_in(n, ends[-1 - k]) for k in range(1, 5)) for n in ringed}
by_ring = lambda x: (ORDER.index(x["ring"]), x["name"])
cards["reach"] = dict(missed=sorted((tag(n) for n in ringed if n not in now_set and prev[n] >= 3), key=by_ring),
                      fresh=sorted((tag(n) for n in now_set if prev[n] == 0), key=by_ring))

# ---------- most in touch ----------
def top(days_back, n_rows=20):
    lo = T - dt.timedelta(days=days_back - 1); rows = []
    for n, p in P.items():
        if rings.get(n) == "Out" and days_back == 30:
            pass
        ds = [d for d in p["contact"] if lo <= d <= T]
        if not ds: continue
        cnt = lambda k: sum(1 for d in p["ch"][k] if lo <= d <= T)
        rows.append(dict(name=n, ring=rings.get(n, ""), days=len(ds), text=cnt("text"), wa=cnt("whatsapp"),
                         call=cnt("call"), cal=cnt("calendar")))
    rows.sort(key=lambda x: -x["days"]); return rows[:n_rows]


TOP = dict(d30=top(30), d12=top(365),
           note="Days in touch counts a day once, however many ways you talked. Call history fills in week by week from 24 Sep 2026.")
unringed_heavy = [r["name"] for r in TOP["d30"] if not r["ring"] and r["days"] >= 8 and not r["name"].startswith("(unsaved)")]

WEEKLY = dict(weeks=weeks, movers=movers[:12], heat=heat, gaps=gaps, cards=cards, targets=CFG["targets_pct"], top=TOP,
              outside=load("outside.json", {"rows": [], "note": ""}),
              note="Your rings are applied to every week, even though you set them on 16 Oct 2026.",
              coverage=[["Texts", "Every one-to-one text and voice memo."],
                        ["WhatsApp", "One-to-one chats with people in your contacts."],
                        ["Calls", "Phone and FaceTime. History builds week by week from 24 Sep 2026."],
                        ["Email", "Days you and each person emailed."],
                        ["Calendar", "Days you met, when there was an invite or their name was on the event."],
                        ["Your taps", "Anything you log as a call, voice memo or in person."],
                        ["Can't see", "Slack, Instagram and LinkedIn messages, or time together nobody texted about."]])

# ---------- picks for the week + nudge ----------
def urgent_now(o):
    return (o["turn"] and o["since_me"] is not None and o["since_me"] >= TH[o["ring"]]) or (o["state"] == "dormant" and o["ring"] in ("Inner", "Close"))


def parked(n):
    a = acts.get(n) or {}
    return a.get("rest") or (a.get("snoozeUntil") and a["snoozeUntil"] > str(T))


turns = sorted([o for o in out if o["turn"] and not parked(o["name"])],
               key=lambda o: (-urgent_now(o), ORDER.index(o["ring"]), -(o["since_me"] or 0)))
first = lambda n: n.split(" (")[0].split(" ")[0]
picks = [dict(name=o["name"], ring=o["ring"],
              nudge=f"{first(o['name'])} wrote you on {label(D(o['turn_date']))}. Your turn.") for o in turns[:3]]
json.dump(dict(built=str(T), picks=picks), open(os.path.join(DATA, "picks.json"), "w"), indent=1)
ph = [w for w in load("picks-history.json", []) if w["built"] != str(T)]
for w in ph:
    for p in w["picks"]:
        a = acts.get(p["name"]) or {}
        if a.get("done") and a["done"] >= w["built"]:
            p["done"] = True
ph.append(dict(built=str(T), picks=[{"name": p["name"]} for p in picks]))
json.dump(ph[-60:], open(os.path.join(DATA, "picks-history.json"), "w"), indent=1)

size = {r: sum(1 for n in ringed if rings[n] == r) for r in ORDER}
live = {r: sum(1 for o in out if o["ring"] == r and o["state"] == "live") for r in ORDER}
json.dump(dict(built=str(T), size=size, live=live, live_pct={r: round(live[r] / size[r] * 100) if size[r] else 0 for r in ORDER},
               targets=CFG["targets_pct"], caps=CFG["caps"], waiting=sum(o["turn"] for o in out),
               unringed_heavy=unringed_heavy, not_found=sum(o["state"] == "nodata" for o in out),
               artifact_url=CFG["artifact_url"]), open(os.path.join(DATA, "summary.json"), "w"), indent=1)

# ---------- page ----------
os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
page = open(os.path.join(ROOT, "web", "template.html")).read()
for k, v in (("__DATA__", json.dumps(out, ensure_ascii=False)), ("__WEEKLY__", json.dumps(WEEKLY, ensure_ascii=False)), ("__BUILT__", str(T))):
    assert k in page, f"template is missing {k}"
    page = page.replace(k, v)
open(os.path.join(ROOT, "dist", "stay-in-touch.html"), "w").write(page)
print(f"built {T}: {len(out)} ringed people, live {live}, waiting {sum(o['turn'] for o in out)}, picks {[p['name'] for p in picks]}")
