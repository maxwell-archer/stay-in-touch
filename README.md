# Stay in Touch

Maxwell's private relationship app: rings (who he wants close) against reality (texts, WhatsApp, calls, email, calendar), with three picks a week, a weekday text nudge and a monthly report.

- **Live page:** https://claude.ai/artifact/V4uLGVAocgCVVPZvLjng4h (private Claude Artifact; taps are saved in its owner-only database, collection `actions`)
- **Design system:** `~/Library/Mobile Documents/com~apple~CloudDocs/MKB/CRM/Design System/` (v2.0 "Navy wireframe")
- **Rings source of truth:** `data/rings.csv` (copied to the vault at `CRM/rings.csv` after each run)

## How it runs

| What | When | How |
|---|---|---|
| Weekly sync + rebuild + republish | Sunday 7:00 am | Claude scheduled task `stay-in-touch-weekly` (needs the Claude app open; runs on next launch if closed) |
| Monthly report to maxwell@archerroad.co | First Sunday of the month, inside the weekly run | `scripts/report.py`, sent from Gmail. `config.json` → `report.mode` is `draft` until Maxwell approves the first one, then `send` |
| Weekday nudge | Mon–Fri 12:30 pm | launchd `co.archerroad.stayintouch.nudge` → `scripts/nudge.py`. Texts Maxwell's own number only. Off until `config.json` → `nudge.enabled` is true and the plist is loaded |

## Files

- `collect/collect_local.py`: reads chat.db, WhatsApp, call history and Contacts (copies, read-only) → `data/history.json`, `groups.json`, `handles.json`; keeps `data/calls.sqlite` so call history outlives the Mac's 200-call limit. Needs Full Disk Access for the process that runs it.
- `build.py`: everything in `data/` → `dist/stay-in-touch.html`, `data/picks.json`, `data/summary.json`, `data/picks-history.json`.
- `web/template.html`: the page. Placeholders `__DATA__`, `__WEEKLY__`, `__BUILT__`.
- `scripts/report.py`, `scripts/nudge.py` (`--dry` prints instead of sending).

`data/` and `dist/` are gitignored: no personal data goes to GitHub.

## Manual run

```
python3 collect/collect_local.py && python3 build.py
```
Then publish `dist/stay-in-touch.html` to the artifact URL with Claude (keep its capabilities).

## Turn on the nudge

```
cp launchd/co.archerroad.stayintouch.nudge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/co.archerroad.stayintouch.nudge.plist
```
and set `nudge.enabled` to true in `config.json`. Messages needs Automation permission for python3 the first time.
