"""Monthly Stay in Touch report: writes data/report-YYYY-MM.{txt,html}.

Run after build.py on the first Sunday of a month. Reads data/summary.json
(written by build.py), data/report-history.json (one entry per month) and
data/picks-history.json. The weekly task sends or drafts the email; this
script only writes the files.
"""
import datetime as dt, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RINGS = ["Inner", "Close", "Keep", "Wide"]


def load(n, d):
    p = os.path.join(DATA, n)
    return json.load(open(p)) if os.path.exists(p) else d


def main():
    s = load("summary.json", None)
    if not s:
        sys.exit("run build.py first")
    today = dt.date.fromisoformat(s["built"])
    month = today.strftime("%Y-%m")
    hist = load("report-history.json", [])
    prev = next((h for h in reversed(hist) if h["month"] < month), None)
    picks_hist = load("picks-history.json", [])
    since = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)  # start of last month
    weeks = [w for w in picks_hist if str(since) <= w["built"] < str(today)]
    picked = sum(len(w["picks"]) for w in weeks)
    done = sum(1 for w in weeks for p in w["picks"] if p.get("done"))
    seen = {}
    for w in weeks:
        for p in w["picks"]:
            if not p.get("done"):
                seen[p["name"]] = seen.get(p["name"], 0) + 1
    skipped = sorted(n for n, c in seen.items() if c >= 2)

    rows, lines = [], []
    for r in RINGS:
        now = s["live_pct"][r]
        was = prev["live_pct"][r] if prev else None
        delta = "" if was is None else (f"+{now - was}" if now > was else (f"{now - was}" if now < was else "same"))
        rows.append((r, f'{s["live"][r]} / {s["size"][r]}', f"{now}%", delta or "first month", f'{s["targets"][r]}%'))
        lines.append(f"  {r:<6} {s['live'][r]:>3} of {s['size'][r]:<3} {now:>3}%  {delta or 'first month'}  (target {s['targets'][r]}%)")

    notes = []
    notes.append(f"{s['waiting']} people are waiting on a reply from you.")
    if picked:
        notes.append(f"You cleared {done} of {picked} weekly picks.")
    if skipped:
        notes.append("Skipped two weeks or more: " + ", ".join(skipped) + ". Still the right ring?")
    for r in RINGS:
        if s["size"][r] >= s["caps"][r]:
            notes.append(f"Your {r} ring is full ({s['size'][r]} of {s['caps'][r]}). Adding someone means moving someone out.")
    if s.get("unringed_heavy"):
        notes.append("In touch a lot but in no ring: " + ", ".join(s["unringed_heavy"]) + ".")
    if s.get("not_found"):
        notes.append(f"I couldn't find {s['not_found']} of your people anywhere. Worth linking their contact cards.")
    question = "Did you see anyone in person this month who isn't reflected here? Just reply with their name."

    subject = f"Stay in Touch · {today.strftime('%B %Y')}"
    txt = "\n".join([subject, "", "How much of each ring you've heard from:", *lines, "", *[f"- {n}" for n in notes], "", question, "", f"Open the app: {s['artifact_url']}", 'Reply "pause" to stop these.'])
    td = 'style="padding:6px 12px 6px 0;border-bottom:1px solid #e3e6ec;font:14px -apple-system,Helvetica,Arial,sans-serif;color:#1f2a44"'
    th = td.replace("color:#1f2a44", "color:#5b6475;font-weight:600")
    html = (
        '<div style="max-width:600px;font:15px -apple-system,Helvetica,Arial,sans-serif;color:#1f2a44">'
        f'<div style="background:#1d2b54;color:#fff;padding:16px 20px;font-size:18px">{subject}</div>'
        '<div style="padding:16px 20px">'
        '<p style="margin:0 0 12px">How much of each ring you\'ve heard from:</p>'
        f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%"><tr><th align="left" {th}>Ring</th><th align="left" {th}>In touch</th><th align="left" {th}>Share</th><th align="left" {th}>vs last month</th><th align="left" {th}>Target</th></tr>'
        + "".join(f"<tr>{''.join(f'<td {td}>{c}</td>' for c in row)}</tr>" for row in rows)
        + "</table><ul style=\"padding-left:18px;line-height:1.5\">" + "".join(f"<li>{n}</li>" for n in notes) + "</ul>"
        f"<p>{question}</p>"
        f'<p><a href="{s["artifact_url"]}" style="color:#1d2b54">Open the app</a></p>'
        '<p style="color:#7a8291;font-size:13px">Reply "pause" to stop these.</p></div></div>')
    open(os.path.join(DATA, f"report-{month}.txt"), "w").write(txt)
    open(os.path.join(DATA, f"report-{month}.html"), "w").write(html)
    json.dump({"subject": subject, "to": json.load(open(os.path.join(ROOT, "config.json")))["report"]["to"]}, open(os.path.join(DATA, f"report-{month}.meta.json"), "w"))
    hist = [h for h in hist if h["month"] != month] + [{"month": month, "live_pct": s["live_pct"]}]
    json.dump(hist, open(os.path.join(DATA, "report-history.json"), "w"), indent=1)
    print(txt)


if __name__ == "__main__":
    main()
