# CFB Slate

Phil's weekly college football TV guide: every FBS and FCS game Thursday through Monday, with channel, kickoff, and the DraftKings line, sorted by his streaming package. One day on screen at a time.

- `index.html` is the live page (GitHub Pages serves it at the repo's Pages URL).
- `build_slate.py` pulls ESPN's public scoreboard API and writes the page. No keys, nothing to install.
- `cfb-slate-weekN.html` is each week's build; the newest one is also the template for the next.
- `summary.txt` is the last build's summary: counts by day, ranked matchups with lines, anything missing.
- `.github/workflows/build.yml` rebuilds Thursday 6:15 AM Arizona and Saturday 6:00 AM Arizona, and on demand from the Actions tab.

Package defaults, carriage assumptions, and channel names live at the top of `build_slate.py`.
