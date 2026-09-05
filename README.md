# Football Slate

Phil's weekly football TV guides: every game with channel, kickoff, and the DraftKings line, laid out like a TV grid, one day on screen at a time, with live scores on game days.

- College: https://pjones7-del.github.io/football-slate/cfb/ (every FBS and FCS game, Thursday through Monday)
- NFL: https://pjones7-del.github.io/football-slate/nfl/ (Wednesday through Monday, with a Market setting for the regional Sunday games on CBS and FOX)
- The root page just links to both.

Files:

- `build_slate.py` pulls ESPN's public scoreboard API and writes both boards. No keys, nothing to install. `--league cfb` or `--league nfl` builds one.
- `cfb-slate-weekN.html` is the college board for week N and the renderer template for both boards; renderer changes go in the newest one.
- `nfl-slate-weekN.html` is the NFL board for week N (an output, not a template).
- `cfb/index.html` and `nfl/index.html` are the live pages; `cfb/summary.txt` and `nfl/summary.txt` are each board's last build summary, `summary.txt` has both.
- `.github/workflows/build.yml` rebuilds every 15 minutes and on demand from the Actions tab.

Package defaults, carriage assumptions, channel names, and the conference and division tables live at the top of `build_slate.py`.
