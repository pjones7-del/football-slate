#!/usr/bin/env python3
"""
build_slate.py - build the weekly football slate pages (college and NFL) from ESPN's public scoreboard API.

College: every FBS and FCS game from Thursday through Monday, grid on Saturday.
NFL: every game from Wednesday through Monday, grid on Sunday.
For each game: broadcast (national TV vs streaming vs nothing), DraftKings spread and total, rank (college) or
record (NFL), team colors and conference or division. The data block goes into the slate template, which is one
renderer for both boards (it reads SLATE.league).

Usage:
  python3 build_slate.py                        # both boards for this week
  python3 build_slate.py --league nfl           # just the NFL
  python3 build_slate.py --main 2026-09-12      # a specific grid day (use with --league)
  python3 build_slate.py --template cfb-slate-week1.html --index index.html --summary summary.txt --artifact none

Template: any cfb-slate-week*.html (default: the newest here). Only the block between /*DATA-START*/ and /*DATA-END*/
is replaced, so renderer changes go in that one file and both boards pick them up. Outputs per league:
  cfb-slate-weekN.html / nfl-slate-weekN.html   the weekly file (also the next build's source of closing lines)
  cfb-slate-artifact.html / nfl-slate-artifact.html   the same page without the document skeleton, fixed title,
                                                       which is what gets published to the hosted artifacts
                                                       (--artifact none to skip)
--index index.html writes the web copies: cfb/index.html and nfl/index.html beside it, plus a small landing page at
index.html itself that links to both. --summary summary.txt writes cfb/summary.txt and nfl/summary.txt beside it
and both summaries into summary.txt. The GitHub Actions workflow passes both flags and nothing else changes for it.
Needs Python 3.9+ and internet access. No packages to install.
"""
import argparse, glob, json, os, re, sys, urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------- college
CFB_API = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?dates={d}&groups={g}&limit=300"
CFB_GROUPS = ("80", "81")  # 80 = FBS, 81 = FCS

# ESPN conferenceId -> (code, name) used by the renderer. The page's Conf picker lists FBS and FCS as two groups,
# in the order below (FCS alphabetical). 26 and 40 are the old OVC and Big South ids, kept in case ESPN still uses them.
FBS_CONF = {"8": ("SEC", "SEC"), "5": ("B1G", "Big Ten"), "1": ("ACC", "ACC"), "4": ("B12", "Big 12"), "151": ("AAC", "AAC"),
            "9": ("PAC", "Pac-12"), "17": ("MWC", "MWC"), "15": ("MAC", "MAC"), "37": ("SBC", "Sun Belt"), "12": ("CUSA", "CUSA"),
            "18": ("IND", "Ind")}
FCS_CONF = {"20": ("BSKY", "Big Sky"), "179": ("BSO", "Big South-OVC"), "26": ("BSO", "Big South-OVC"), "40": ("BSO", "Big South-OVC"),
            "48": ("CAA", "CAA"), "32": ("FCSI", "FCS Ind"), "22": ("IVY", "Ivy"), "24": ("MEAC", "MEAC"), "21": ("MVFC", "MVFC"),
            "25": ("NEC", "NEC"), "27": ("PAT", "Patriot"), "28": ("PFL", "Pioneer"), "29": ("SOCON", "SoCon"),
            "30": ("SLC", "Southland"), "31": ("SWAC", "SWAC"), "177": ("UAC", "UAC")}


def cfb_conf_table():
    """[[code, name, level], ...] in picker order: FBS as listed, FCS alphabetical, then the non-Division I bucket."""
    rows = [[c, n, "fbs"] for c, n in FBS_CONF.values()]
    seen = set()
    for c, n in sorted(FCS_CONF.values(), key=lambda x: x[1].lower()):
        if c not in seen:
            seen.add(c)
            rows.append([c, n, "fcs"])
    rows.append(["OTHER", "Non-D1", "other"])
    return rows


def cfb_conf_code(cid):
    if cid in FBS_CONF:
        return FBS_CONF[cid][0]
    if cid in FCS_CONF:
        return FCS_CONF[cid][0]
    return "OTHER"


# grid row order for national TV channels; anything else national-TV is appended alphabetically
CFB_TV_ORDER = ["ABC", "CBS", "FOX", "NBC", "CW", "ESPN", "ESPN2", "TNT", "ESPNU", "FS1", "FS2", "USA", "ACCN", "SECN", "BTN", "CBSSN"]
# streaming services and regional feeds, in grid row order after the TV channels (ESPN+ last because it stacks deepest)
CFB_STREAM_ORDER = ["Peacock", "Disney+", "HBO Max", "Paramount+", "Fox One", "SECN+", "ACCNX", "MW+", "YouTube", "HBCU Go", "ESPN3", "ESPN+"]
# what each package carries, as ESPN's channel names. Edit here when carriage changes.
LINEAR = ["ABC", "CBS", "FOX", "NBC", "CW", "ESPN", "ESPN2", "ESPNU", "ESPNEWS", "SECN", "ACCN", "BTN",
          "FS1", "FS2", "CBSSN", "USA", "TNT", "TBS", "TRUTV", "NBCSN"]
CFB_PACKAGES = {
    "YouTube TV":        LINEAR,
    "Hulu + Live TV":    [n for n in LINEAR if n != "CW"] + ["ESPN+", "Disney+"],
    "Fubo":              [n for n in LINEAR if n not in ("TNT", "TBS", "TRUTV")],
    "Cable / satellite": LINEAR,
    "Antenna":           ["ABC", "CBS", "FOX", "NBC", "CW"],
    "ESPN Unlimited":    ["ESPN", "ESPN2", "ESPNU", "ESPNEWS", "SECN", "ACCN", "ESPN+", "SECN+", "ACCNX", "ESPN3"],
    "ESPN+ only":        ["ESPN+", "ESPN3"],
    "Peacock":           ["Peacock", "NBC"],
    "Paramount+":        ["Paramount+", "CBS"],
    "Disney+":           ["Disney+"],
    "HBO Max":           ["HBO Max", "TNT"],
    "Fox One":           ["FOX", "FS1", "FS2"],
    "MW+":               ["MW+"],
    "Free apps":         ["CW", "YouTube", "HBCU Go"],
}
NAME_ALIAS = {"Hawai'i": "Hawaii", "California": "Cal", "Pittsburgh": "Pitt", "Miami": "Miami (FL)",
              "Long Island University": "LIU", "Massachusetts": "UMass"}

# ---------------------------------------------------------------- NFL
NFL_API = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={d}&limit=100"
NFL_DIV = {  # ESPN abbreviation -> division code; the renderer's Div picker groups these by conference
    "BUF": "AFCE", "MIA": "AFCE", "NE": "AFCE", "NYJ": "AFCE",
    "BAL": "AFCN", "CIN": "AFCN", "CLE": "AFCN", "PIT": "AFCN",
    "HOU": "AFCS", "IND": "AFCS", "JAX": "AFCS", "TEN": "AFCS",
    "DEN": "AFCW", "KC": "AFCW", "LV": "AFCW", "LAC": "AFCW",
    "DAL": "NFCE", "NYG": "NFCE", "PHI": "NFCE", "WSH": "NFCE",
    "CHI": "NFCN", "DET": "NFCN", "GB": "NFCN", "MIN": "NFCN",
    "ATL": "NFCS", "CAR": "NFCS", "NO": "NFCS", "TB": "NFCS",
    "ARI": "NFCW", "LAR": "NFCW", "SF": "NFCW", "SEA": "NFCW",
}
NFL_CONFS = [["AFCE", "AFC East", "afc"], ["AFCN", "AFC North", "afc"], ["AFCS", "AFC South", "afc"], ["AFCW", "AFC West", "afc"],
             ["NFCE", "NFC East", "nfc"], ["NFCN", "NFC North", "nfc"], ["NFCS", "NFC South", "nfc"], ["NFCW", "NFC West", "nfc"]]
NFL_TV_ORDER = ["CBS", "FOX", "NBC", "ABC", "ESPN", "ESPN2", "NFL Network"]
NFL_STREAM_ORDER = ["Prime Video", "Peacock", "Netflix", "YouTube", "ESPN+", "NFL+"]
NFL_LINEAR = ["CBS", "FOX", "NBC", "ABC", "ESPN", "ESPN2", "NFL Network"]
NFL_PACKAGES = {
    "YouTube TV":        NFL_LINEAR,
    "Hulu + Live TV":    NFL_LINEAR + ["ESPN+"],
    "Fubo":              NFL_LINEAR,
    "Cable / satellite": NFL_LINEAR,
    "Antenna":           ["CBS", "FOX", "NBC", "ABC"],
    "ESPN Unlimited":    ["ESPN", "ESPN2", "ESPN+"],
    "Prime Video":       ["Prime Video"],
    "Peacock":           ["Peacock", "NBC"],
    "Paramount+":        ["CBS"],
    "Netflix":           ["Netflix"],
    "Free apps":         ["YouTube"],
}

NET_ALIAS = {"SEC Network": "SECN", "ACC Network": "ACCN", "USA Net": "USA", "USA Network": "USA",
             "Big Ten Network": "BTN", "CBS Sports Network": "CBSSN", "The CW": "CW", "CW Network": "CW",
             "TNT/HBO Max": "TNT", "truTV": "TRUTV", "ESPNews": "ESPNEWS", "ESPN News": "ESPNEWS", "NBC Sports Network": "NBCSN",
             "NFL Net": "NFL Network", "NFLN": "NFL Network", "NFL Network": "NFL Network", "Amazon Prime Video": "Prime Video",
             "Prime": "Prime Video", "Amazon Prime": "Prime Video"}
STREAM_TYPES = {"Streaming", "Web"}
DEFAULT_PACKAGES = "everything"   # what a board opens with: "everything" = every channel and service with a game that week,
                                  # or a list of package names, e.g. ["YouTube TV", "Peacock", "ESPN Unlimited"]

LEAGUES = {
    "cfb": {"name": "College Football", "api": CFB_API, "groups": CFB_GROUPS, "file": "cfb-slate", "title": "CFB Slate",
            "logo": "/i/teamlogos/ncaa/500/{lg}.png", "main_wd": 5, "before": 2, "after": 2,
            "tv": CFB_TV_ORDER, "stream": CFB_STREAM_ORDER, "packages": CFB_PACKAGES},
    "nfl": {"name": "NFL", "api": NFL_API, "groups": ("",), "file": "nfl-slate", "title": "NFL Slate",
            "logo": "/i/teamlogos/nfl/500/{lg}.png", "main_wd": 6, "before": 4, "after": 1,
            "tv": NFL_TV_ORDER, "stream": NFL_STREAM_ORDER, "packages": NFL_PACKAGES},
}


def fetch(api, d, g):
    req = urllib.request.Request(api.format(d=d, g=g))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def lum(hexc):
    hexc = hexc.lstrip("#")
    if len(hexc) != 6:
        return 0
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def colors(team):
    c = (team.get("color") or "3b4560").lower()
    a = (team.get("alternateColor") or "").lower()
    if lum(c) > 0.8 and a and lum(a) < lum(c):  # near-white primary: use the alternate
        c = a
    fg = "#111" if lum(c) > 0.2 else "#fff"
    return "#" + c, fg


def dist(a, b):
    a, b = a.lstrip("#"), b.lstrip("#")
    return sum((int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) ** 2 for i in (0, 2, 4)) ** 0.5


def label(name):
    return name.upper().replace(" STATE", " ST")


def norm_net(n):
    return NET_ALIAS.get(n, n)


def live_state(event, comp):
    """{'st': 'in'|'post', 'as': away score, 'hs': home score, 'clk': 'Final' or '8:41 - 2nd'} for a started game,
    else None. Same shape the page builds from ESPN in the browser, so the build is just the opening snapshot."""
    st = (event.get("status") or comp.get("status") or {})
    typ = st.get("type") or {}
    state = typ.get("state")
    if state not in ("in", "post"):
        return None
    scores = {}
    for side in comp.get("competitors", []):
        try:
            scores[side["homeAway"]] = int(float(side.get("score") or 0))
        except (TypeError, ValueError):
            scores[side["homeAway"]] = 0
    return {"st": state, "as": scores.get("away", 0), "hs": scores.get("home", 0), "clk": typ.get("shortDetail") or ("Final" if state == "post" else "")}


def previous_lines(html):
    """{event id: (spread, total)} from a slate file's data block, for games that had a line last build."""
    out = {}
    m = re.search(r"/\*DATA-START\*/(.*?)/\*DATA-END\*/", html, re.S)
    if not m:
        return out
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(",")
        if line.startswith('{"id":'):
            try:
                g = json.loads(line)
            except ValueError:
                continue
            if g.get("sp") is not None or g.get("ou") is not None:
                out[str(g["id"])] = (g.get("sp"), g.get("ou"))
    return out


def artifact_copy(html, title):
    """The hosted artifact viewer supplies doctype, html, head and body, so keep only the title, the styles and the
    body content. The title stays fixed so the artifact keeps its name from week to week."""
    style = re.search(r"<style>.*?</style>", html, re.S)
    body = re.search(r"<body>(.*)</body>", html, re.S)
    if not (style and body):
        sys.exit("could not find <style> and <body> in the output; artifact copy not written")
    return f"<title>{title}</title>\n{style.group(0)}\n{body.group(1).strip()}\n"


def newest(pattern, exclude=()):
    """Newest file matching the glob: highest week number in the name, then modification time."""
    def key(f):
        m = re.search(r"(\d+)\.html$", f)
        return (int(m.group(1)) if m else 0, os.path.getmtime(f))
    cands = sorted((f for f in glob.glob(pattern) if os.path.basename(f) not in exclude), key=key)
    return cands[-1] if cands else None


def main_day_for(league, today):
    """Grid day: the league's main weekday (Saturday or Sunday). The days after it through Monday still belong to the
    weekend just played; from Tuesday it rolls forward. Matters for the every-15-minutes rebuild."""
    main_wd, wd = LEAGUES[league]["main_wd"], today.weekday()  # Mon=0 .. Sun=6
    back = (wd - main_wd) % 7
    if 0 < back <= (0 - main_wd) % 7:
        return today - timedelta(days=back)
    return today + timedelta(days=(main_wd - wd) % 7)


def week_label(data, main_day, nfl=False):
    """'Week 7', or the calendar's name for an NFL postseason week ('Wild Card'), or the week of the grid day."""
    wk = (data.get("week") or {}).get("number")
    stype = ((data.get("season") or {}).get("type"))
    if wk is not None and stype == 3 and nfl:
        for cal in (data.get("leagues") or [{}])[0].get("calendar", []):
            for en in (cal.get("entries") or []) if isinstance(cal, dict) else []:
                if str(en.get("value")) == str(wk) and "Post" in str(cal.get("label", "")):
                    return en.get("label") or f"Playoffs {wk}", f"post{wk}"
        return f"Playoffs {wk}", f"post{wk}"
    if wk is not None:
        return f"Week {wk}", f"week{wk}"
    return main_day.strftime("Week of %b %-d"), main_day.strftime("%Y%m%d")


def build(league, tpl, prev_lines, main_day, start, end):
    """Fetch one league's week and return (html, summary lines, week slug)."""
    L = LEAGUES[league]
    nfl = league == "nfl"
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    events, week, slug, provider = {}, None, None, None
    for d in dates:
        for g in L["groups"]:
            data = fetch(L["api"], d.strftime("%Y%m%d"), g)
            if (d == main_day or week is None) and g == L["groups"][0] and (data.get("week") or {}).get("number") is not None:
                week, slug = week_label(data, main_day, nfl)
            for e in data.get("events", []):
                if e.get("id") and e.get("competitions"):
                    events.setdefault(e["id"], e)
    if week is None:
        week, slug = week_label({}, main_day, nfl)
    if not events:
        raise LookupError(f"no games between {start} and {end}")

    teams, days, unknown_conf = {}, {d: [] for d in dates}, {}
    for e in events.values():
        c = e["competitions"][0]
        dt = datetime.fromisoformat(c["date"].replace("Z", "+00:00")).astimezone(ET)
        day = dt.date()
        hh = dt.hour
        if hh < 6:  # a game after midnight ET belongs to the previous day's slate
            day -= timedelta(days=1)
            hh += 24
        if day not in days:
            continue
        tbd = c.get("timeValid") is False
        home = away = None
        for comp in c["competitors"]:
            t = comp["team"]
            if nfl:
                name = t.get("displayName") or f"{t.get('location', '')} {t.get('name', '')}".strip()
                code = NFL_DIV.get(t.get("abbreviation", ""), "OTHER")
            else:
                name = NAME_ALIAS.get(t["location"], t["location"])
                cid = str(t.get("conferenceId", ""))
                code = cfb_conf_code(cid)
                if code == "OTHER":
                    unknown_conf.setdefault(cid or "none", set()).add(name)
            if name not in teams:
                bg, fg = colors(t)
                teams[name] = {"l": (t.get("name") or name).upper() if nfl else label(name), "c": code, "ab": t.get("abbreviation", name), "bg": bg, "fg": fg}
                if nfl:
                    teams[name]["lg"] = t.get("abbreviation", "").lower()   # ESPN's NFL logo files are named by abbreviation
                elif t.get("logo") and t.get("id"):
                    teams[name]["lg"] = str(t["id"])   # college: ESPN team id; the page builds the logo URL from it
                alt_c = (t.get("alternateColor") or "").lower()
                if len(alt_c) == 6 and alt_c != bg.lstrip("#"):
                    teams[name]["_alt"] = "#" + alt_c
            rank = (comp.get("curatedRank") or {}).get("current", 99)
            rec = ((comp.get("records") or [{}])[0].get("summary") or "") if nfl else ""
            side = {"name": name, "rank": rank if rank and rank <= 25 else 0, "rec": rec if rec and rec != "0-0" else ""}
            if comp["homeAway"] == "home":
                home = side
            else:
                away = side
        if not home or not away:
            continue

        tv_nat, stream_nat, other = [], [], []
        for gb in c.get("geoBroadcasts", []):
            typ, mkt = gb["type"]["shortName"], gb["market"]["type"]
            n = norm_net(gb["media"]["shortName"])
            if typ == "Radio":
                continue
            if typ == "TV" and mkt == "National":
                tv_nat.append(n)
            elif typ in STREAM_TYPES and mkt == "National":
                stream_nat.append(n)
            else:
                other.append(n)
        if not (tv_nat or stream_nat or other):
            for b in c.get("broadcasts", []):
                for n in b.get("names", []):
                    n = norm_net(n)
                    (tv_nat if n in L["tv"] else stream_nat).append(n)
        chosen = (tv_nat or stream_nat or other or [""])[0]
        alt = []
        for n in tv_nat + stream_nat + other:
            if n != chosen and n not in alt:
                alt.append(n)

        odds = (c.get("odds") or [None])[0]
        sp = ou = None
        if odds:
            provider = provider or ((odds.get("provider") or {}).get("name") or "").replace("Draft Kings", "DraftKings")
            sp, ou = odds.get("spread"), odds.get("overUnder")

        v = c.get("venue") or {}
        adr = v.get("address") or {}
        note = ""
        if c.get("neutralSite"):
            where = adr.get("state") if adr.get("country") in (None, "", "USA", "United States") else adr.get("country")
            note = ", ".join(x for x in (adr.get("city"), where) if x) or v.get("fullName", "")

        game = {"id": e["id"], "n": chosen, "tv": bool(tv_nat), "k": f"{hh:02d}:{dt.minute:02d}", "a": away["name"], "h": home["name"],
                "ar": away["rank"], "hr": home["rank"], "neu": bool(c.get("neutralSite")), "note": note,
                "sp": sp, "ou": ou}
        if away["rec"] or home["rec"]:
            game["arec"], game["hrec"] = away["rec"], home["rec"]
        if alt:
            game["alt"] = alt
        # build-time snapshot of a started game: state (in/post), scores, clock text. The page refreshes these live.
        live = live_state(e, c)
        if live:
            game["live"] = live
            if sp is None and ou is None and e["id"] in prev_lines:
                # ESPN pulls the odds at kickoff; keep the last pregame number as the closing line
                game["sp"], game["ou"] = prev_lines[e["id"]]
                game["cl"] = True
        hb, ab_ = teams[home["name"]], teams[away["name"]]
        if dist(hb["bg"], ab_["bg"]) < 70 and hb.get("_alt") and dist(hb["_alt"], ab_["bg"]) >= 70:
            game["hc"] = [hb["_alt"], "#111" if lum(hb["_alt"]) > 0.2 else "#fff"]
        if tbd:
            game["tbd"] = True
        days[day].append(game)

    for d in days:
        days[d].sort(key=lambda g: (g["k"], not g["tv"], g["n"], g["h"]))

    all_present = {n for gs in days.values() for g in gs for n in [g["n"]] + g.get("alt", []) if n}
    nets_present = {g["n"] for gs in days.values() for g in gs if g["tv"]}
    networks = [n for n in L["tv"] if n in nets_present] + sorted(n for n in nets_present if n not in L["tv"])
    order = networks + [n for n in L["stream"] if n in all_present] + sorted(n for n in all_present if n not in networks and n not in L["stream"])
    have = sorted(all_present) if DEFAULT_PACKAGES == "everything" else sorted({n for p in DEFAULT_PACKAGES for n in L["packages"].get(p, [])})
    pulled = datetime.now(ET).strftime("%a %-m/%-d, %-I:%M %p ET")
    slate = {
        "league": league,
        "name": L["name"],
        "logo": L["logo"],
        "week": week,
        "main": main_day.isoformat(),
        "mainLabel": main_day.strftime("%a, %b %-d"),
        "pulled": pulled,
        "lines": f"{provider} via ESPN" if provider else "unavailable",
        "networks": networks,
        "order": order,
        "packages": L["packages"],
        "have": have,
        "confs": NFL_CONFS if nfl else cfb_conf_table(),
        "days": [{"label": d.strftime("%a %-m/%-d"), "date": d.isoformat(), "games": days[d]} for d in dates if days[d]],
    }

    def js(o):
        return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    lines = ["const SLATE = {"]
    for k in ("league", "name", "logo", "week", "main", "mainLabel", "pulled", "lines", "networks", "order", "packages", "have", "confs"):
        lines.append(f"  {js(k)}: {js(slate[k])},")
    lines.append('  "days": [')
    for i, d in enumerate(slate["days"]):
        lines.append(f'    {{"label": {js(d["label"])}, "date": {js(d["date"])}, "games": [')
        for j, g in enumerate(d["games"]):
            lines.append("      " + js(g) + ("," if j < len(d["games"]) - 1 else ""))
        lines.append("    ]}" + ("," if i < len(slate["days"]) - 1 else ""))
    lines.append("  ]")
    lines.append("};")
    lines.append("const TEAMS = {")
    names = sorted(teams)
    for i, n in enumerate(names):
        rec = {k: v for k, v in teams[n].items() if not k.startswith("_")}
        lines.append(f"  {js(n)}: {js(rec)}" + ("," if i < len(names) - 1 else ""))
    lines.append("};")
    block = "/*DATA-START*/\n/* Generated by build_slate.py. Do not hand-edit unless the script can't run;\n   see cfb-slate-handoff.md for the field reference. */\n" + "\n".join(lines) + "\n/*DATA-END*/"

    out_html, n = re.subn(r"/\*DATA-START\*/.*?/\*DATA-END\*/", lambda m: block, tpl, count=1, flags=re.S)
    if n != 1:
        sys.exit("template is missing the /*DATA-START*/ ... /*DATA-END*/ markers")
    out_html = re.sub(r"<title>.*?</title>", f"<title>{L['title']} - {week}</title>", out_html, count=1)

    # summary
    rep = []
    total = sum(len(g) for g in days.values())
    rep.append(f"{L['name']} {week} ({total} games, pulled {pulled}, lines {slate['lines']})")
    for d in dates:
        gs = days[d]
        if gs:
            rep.append(f"  {d.strftime('%a %-m/%-d')}: {sum(g['tv'] for g in gs)} on TV, {sum(not g['tv'] for g in gs)} not")
    rk = lambda r: f"#{r} " if r else ""

    def line_of(g):
        if g["sp"] is None:
            return ""
        fav = g["h"] if g["sp"] < 0 else g["a"] if g["sp"] > 0 else None
        s = f"  {teams[fav]['ab']} -{abs(g['sp'])}" if fav else "  PK"
        if g["ou"] is not None:
            s += f", o/u {g['ou']}"
        return s
    listed = [(d, g) for d in dates for g in days[d] if nfl or g["ar"] or g["hr"]]
    rep.append("  every game with its line:" if nfl else "  ranked matchups:")
    for d, g in listed:
        rep.append(f"    {d.strftime('%a')} {g['k']} ET  {rk(g['ar'])}{g['a']} {'vs' if g['neu'] else 'at'} {rk(g['hr'])}{g['h']}  [{g['n'] or 'no TV'}]{line_of(g)}")
    nolines = sum(1 for d in dates for g in days[d] if g["sp"] is None and g["tv"])
    rep.append(f"  TV games without a line: {nolines}")
    tbds = [(d, g) for d in dates for g in days[d] if g.get("tbd")]
    rep.append(f"  time TBD: {len(tbds)}" + (": " + "; ".join(f"{g['a']} at {g['h']}" for d, g in tbds) if tbds else ""))
    if unknown_conf:
        rep.append("  non-Division I or unknown conference ids: " + "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in unknown_conf.items()))
    return out_html, rep, slug


LANDING = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Football Slate</title>
<style>html{color-scheme:dark}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b1a33;color:#e6ecf7;
font-family:"Avenir Next Condensed","Roboto Condensed","Arial Narrow","Helvetica Neue",Arial,sans-serif}.box{text-align:center;padding:24px}
h1{margin:0 0 18px;font-size:22px;letter-spacing:.06em;text-transform:uppercase}a{display:block;margin:10px auto;width:260px;padding:16px 0;border:1px solid rgba(255,255,255,.25);
border-radius:8px;color:#e6ecf7;text-decoration:none;font-size:20px;font-weight:700;letter-spacing:.04em}a:hover{background:#10224a}small{display:block;margin-top:14px;color:#9fb0cc;font-size:12px}</style></head>
<body><div class="box"><h1>Football slate</h1><a href="cfb/">College Football</a><a href="nfl/">NFL</a><small>Every game, by channel and kickoff. Lines from DraftKings via ESPN.</small></div></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", choices=["both", "cfb", "nfl"], default="both", help="which board to build (default both)")
    ap.add_argument("--main", help="grid day, YYYY-MM-DD (default: the coming Saturday for college, Sunday for the NFL)")
    ap.add_argument("--start", help="first day to include, YYYY-MM-DD (default: main - 2 college, main - 4 NFL)")
    ap.add_argument("--end", help="last day to include, YYYY-MM-DD (default: main + 2 college, main + 1 NFL)")
    ap.add_argument("--template", help="slate file to reuse the renderer from (default: newest cfb-slate-week*.html here)")
    ap.add_argument("--out", help="output file for a single-league build (default: cfb-slate-weekN.html or nfl-slate-weekN.html)")
    ap.add_argument("--artifact", default="", help="artifact-ready copy: default cfb-slate-artifact.html / nfl-slate-artifact.html; 'none' to skip")
    ap.add_argument("--index", help="web root index file; writes cfb/index.html and nfl/index.html beside it and a landing page at it")
    ap.add_argument("--summary", help="summary file; writes cfb/summary.txt and nfl/summary.txt beside it too")
    args = ap.parse_args()
    leagues = ["cfb", "nfl"] if args.league == "both" else [args.league]

    template = args.template or newest("cfb-slate-*.html", exclude=("cfb-slate-artifact.html",))
    if not template:
        sys.exit("no cfb-slate-week*.html found to use as the template; pass --template")
    tpl = open(template, encoding="utf-8").read()
    today = datetime.now(ET).date()

    reports, web_root = [], os.path.dirname(args.index) if args.index else None
    for league in leagues:
        L = LEAGUES[league]
        main_day = date.fromisoformat(args.main) if args.main else main_day_for(league, today)
        start = date.fromisoformat(args.start) if args.start else main_day - timedelta(days=L["before"])
        end = date.fromisoformat(args.end) if args.end else main_day + timedelta(days=L["after"])
        # last line seen for each game in the previous build of this board (ESPN drops odds at kickoff): the web copy
        # from the last run when there is one, else the newest weekly file (college: the template itself)
        web_prev = os.path.join(web_root, league, "index.html") if web_root is not None else None
        prev_src = (web_prev if web_prev and os.path.exists(web_prev) else
                    template if league == "cfb" else newest("nfl-slate-*.html", exclude=("nfl-slate-artifact.html",)))
        prev_lines = previous_lines(open(prev_src, encoding="utf-8").read()) if prev_src and os.path.exists(prev_src) else {}
        html = None
        for attempt in range(3):   # a window with no games (the gap before the season, a Sunday with none) rolls a week forward
            try:
                html, rep, slug = build(league, tpl, prev_lines, main_day, start, end)
                break
            except LookupError as ex:
                if args.main or attempt == 2:
                    reports.append([f"{L['name']}: {ex}"])
                    break
                main_day, start, end = (x + timedelta(days=7) for x in (main_day, start, end))
            except Exception as ex:   # one board failing shouldn't take the other down
                if len(leagues) == 1:
                    raise
                reports.append([f"{L['name']}: build failed ({ex.__class__.__name__}: {ex})"])
                break
        if html is None:
            continue
        out = args.out if (args.out and len(leagues) == 1) else f"{L['file']}-{slug}.html"
        open(out, "w", encoding="utf-8").write(html)
        extra = [f"  weekly file written to {out}"]
        artifact = args.artifact or f"{L['file']}-artifact.html"
        if artifact != "none":
            open(artifact, "w", encoding="utf-8").write(artifact_copy(html, L["title"]))
            extra.append(f"  artifact copy written to {artifact}")
        if args.index:
            folder = os.path.join(web_root, league)
            os.makedirs(folder, exist_ok=True)
            open(os.path.join(folder, "index.html"), "w", encoding="utf-8").write(html)
            extra.append(f"  web copy written to {os.path.join(folder, 'index.html')}")
        rep[1:1] = extra
        if args.index and args.summary:
            open(os.path.join(web_root, league, "summary.txt"), "w", encoding="utf-8").write("\n".join(rep) + "\n")
        reports.append(rep)
    if args.index:
        open(args.index, "w", encoding="utf-8").write(LANDING)
    text = "\n\n".join("\n".join(r) for r in reports)
    print(text)
    if args.summary:
        open(args.summary, "w", encoding="utf-8").write(text + "\n")


if __name__ == "__main__":
    main()
