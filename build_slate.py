#!/usr/bin/env python3
"""
build_slate.py - build the football slate page (college and NFL in one page) from ESPN's public scoreboard API.

v2 (9/13/2026): one page carries both leagues. cfb/index.html opens on College, nfl/index.html opens on the NFL, and the
College / NFL control switches inside the page. The renderer is the Slate Lab board: team-colored timeline bars with an
anchored scorebug, cards, live possession and down and distance, live odds, channel logos, per-league package,
conference and team filters, saved views, print.

College: every FBS and FCS game from Thursday through Monday, grid on Saturday.
NFL: every game from Wednesday through Monday, grid on Sunday.

Usage:
  python3 build_slate.py                        # both boards for this week
  python3 build_slate.py --league nfl           # just the NFL (the page then carries one league)
  python3 build_slate.py --main 2026-09-12      # a specific grid day (use with --league)
  python3 build_slate.py --template cfb-slate-week2.html --index index.html --summary summary.txt --artifact none

Template: any cfb-slate-week*.html (default: the newest here). Only the block between /*DATA-START*/ and /*DATA-END*/
is replaced, so renderer changes go in that one file. Outputs:
  cfb-slate-weekN.html / nfl-slate-weekN.html   the weekly files: the same page, opening on College and on the NFL
  cfb-slate-artifact.html / nfl-slate-artifact.html   the same without the document skeleton, for the hosted artifacts
                                                       (--artifact none to skip; --embed inlines the logos for them)
--index index.html writes the web copies cfb/index.html and nfl/index.html beside it, plus the landing page at index.html.
--summary summary.txt writes cfb/summary.txt and nfl/summary.txt beside it and both into summary.txt.
Needs Python 3.9+ and internet access. No packages to install (--embed wants Pillow, only used by hand).
"""
import argparse, glob, json, os, re, sys, urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- college
CFB_API = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?dates={d}&groups={g}&limit=300"
CFB_GROUPS = ("80", "81")  # 80 = FBS, 81 = FCS

# ESPN conferenceId -> (code, name). The page's Conf picker lists FBS and FCS as two groups, in the order below (FCS
# alphabetical). 26 and 40 are the old OVC and Big South ids, kept in case ESPN still uses them.
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
CFB_TV_ORDER = ["ABC", "CBS", "FOX", "NBC", "CW", "ESPN", "ESPN2", "TNT", "ESPNU", "FS1", "FS2", "USA", "ACCN", "SECN", "BTN", "CBSSN", "ESPNEWS"]
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
NFL_PACKAGES = {   # fewer than college on purpose: the four live-TV services carry the same NFL channels, so they are one chip
    "Antenna":           ["CBS", "FOX", "NBC", "ABC"],
    "Cable or live TV":  NFL_LINEAR,          # YouTube TV, Hulu + Live TV, Fubo, cable, satellite
    "ESPN Unlimited":    ["ESPN", "ESPN2", "ESPN+"],
    "Peacock":           ["Peacock", "NBC"],
    "Paramount+":        ["Paramount+", "CBS"],
    "Prime Video":       ["Prime Video"],
    "Netflix":           ["Netflix"],
    "YouTube":           ["YouTube"],
    "NFL+":              ["NFL+", "NFL Network"],
}

NET_ALIAS = {"SEC Network": "SECN", "ACC Network": "ACCN", "USA Net": "USA", "USA Network": "USA",
             "Big Ten Network": "BTN", "CBS Sports Network": "CBSSN", "The CW": "CW", "CW Network": "CW",
             "TNT/HBO Max": "TNT", "truTV": "TRUTV", "ESPNews": "ESPNEWS", "ESPN News": "ESPNEWS", "NBC Sports Network": "NBCSN",
             "NFL Net": "NFL Network", "NFLN": "NFL Network", "NFL Network": "NFL Network", "Amazon Prime Video": "Prime Video",
             "Prime": "Prime Video", "Amazon Prime": "Prime Video"}
STREAM_TYPES = {"Streaming", "Web"}
DEFAULT_PACKAGES = "everything"   # what a board opens with: "everything" = every channel and service with a game that week,
                                  # or a list of package names, e.g. ["YouTube TV", "Peacock", "ESPN Unlimited"]
CORE_ODDS = "https://sports.core.api.espn.com/v2/sports/football/leagues/{lg}/events/{id}/competitions/{id}/odds?limit=20"

LEAGUES = {
    "cfb": {"name": "College Football", "api": CFB_API, "groups": CFB_GROUPS, "file": "cfb-slate", "title": "CFB Slate", "core": "college-football",
            "logo": "/i/teamlogos/ncaa/500/{lg}.png", "main_wd": 5, "before": 2, "after": 2,
            "tv": CFB_TV_ORDER, "stream": CFB_STREAM_ORDER, "packages": CFB_PACKAGES},
    "nfl": {"name": "NFL", "api": NFL_API, "groups": ("",), "file": "nfl-slate", "title": "NFL Slate", "core": "nfl",
            "logo": "/i/teamlogos/nfl/500/{lg}.png", "main_wd": 6, "before": 4, "after": 1,
            "tv": NFL_TV_ORDER, "stream": NFL_STREAM_ORDER, "packages": NFL_PACKAGES},
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "football-slate/2.0 (+https://pjones7-del.github.io/football-slate/)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def norm_net(n):
    return NET_ALIAS.get(n, n)


def team(comp, nfl):
    """One side of a game, in the shape the renderer reads (and rebuilds itself from ESPN when it polls)."""
    t = comp["team"]
    if nfl:
        name = t.get("displayName") or f"{t.get('location', '')} {t.get('name', '')}".strip()
        short = t.get("name") or t.get("shortDisplayName") or name
        conf = NFL_DIV.get(t.get("abbreviation", ""), "OTHER")
    else:
        name = NAME_ALIAS.get(t.get("location", ""), t.get("location", ""))
        short = name
        conf = cfb_conf_code(str(t.get("conferenceId", "")))
    rank = (comp.get("curatedRank") or {}).get("current") or 0
    rec = ((comp.get("records") or [{}])[0].get("summary") or "")
    try:
        score = int(float(comp.get("score") or 0))
    except (TypeError, ValueError):
        score = 0
    return {"id": str(t.get("id")), "ab": t.get("abbreviation", ""), "name": name, "short": short,
            "color": (t.get("color") or "3b4560").lower(), "alt": (t.get("alternateColor") or "").lower(),
            "logo": (t.get("abbreviation", "").lower() if nfl else str(t.get("id"))),
            "rank": rank if 0 < rank <= 25 else 0, "rec": rec if rec and rec != "0-0" else "", "conf": conf,
            "score": score, "winner": bool(comp.get("winner")),
            "line": [int(float(x.get("value") or 0)) for x in comp.get("linescores", [])]}


def game(e, L, nfl):
    """One game in the renderer's shape, or None. Broadcasts are classified the way the old board did: national TV
    first, then national streaming, then regional feeds; the first of those is the channel the game files under."""
    c = e["competitions"][0]
    sides = {x["homeAway"]: x for x in c["competitors"]}
    if "home" not in sides or "away" not in sides:
        return None
    st = e.get("status") or c.get("status") or {}
    typ = st.get("type") or {}
    tv_nat, stream_nat, other = [], [], []
    for gb in c.get("geoBroadcasts", []):
        typ_b, mkt = gb["type"]["shortName"], gb["market"]["type"]
        n = norm_net(gb["media"]["shortName"])
        if typ_b == "Radio":
            continue
        if typ_b == "TV" and mkt == "National":
            tv_nat.append(n)
        elif typ_b in STREAM_TYPES and mkt == "National":
            stream_nat.append(n)
        else:
            other.append(n)
    if not (tv_nat or stream_nat or other):
        for b in c.get("broadcasts", []):
            for n in b.get("names", []):
                n = norm_net(n)
                (tv_nat if n in L["tv"] else stream_nat).append(n)
    carriers = []
    for n in tv_nat + stream_nat + other:
        if n and n not in carriers:
            carriers.append(n)
    odds = (c.get("odds") or [None])[0] or {}
    sit = c.get("situation") or {}
    v = c.get("venue") or {}
    adr = v.get("address") or {}
    where = adr.get("state") if adr.get("country") in (None, "", "USA", "United States") else adr.get("country")
    return {
        "id": e["id"], "kick": c["date"], "tbd": c.get("timeValid") is False,
        "away": team(sides["away"], nfl), "home": team(sides["home"], nfl),
        "neutral": bool(c.get("neutralSite")), "venue": ", ".join(x for x in (adr.get("city"), where) if x) or v.get("fullName", ""),
        "carriers": carriers, "tv": bool(tv_nat), "_tv": tv_nat,
        "spread": odds.get("spread"), "ou": odds.get("overUnder"),
        "provider": ((odds.get("provider") or {}).get("name") or "").replace("Draft Kings", "DraftKings"),
        "state": typ.get("state"), "detail": typ.get("shortDetail"), "period": st.get("period"), "clock": st.get("displayClock"),
        "sit": {"poss": sit.get("possession"), "dd": sit.get("shortDownDistanceText"), "at": sit.get("possessionText"),
                "yl": sit.get("yardLine"), "rz": bool(sit.get("isRedZone")), "ht": sit.get("homeTimeouts"), "at_": sit.get("awayTimeouts"),
                "last": ((sit.get("lastPlay") or {}).get("text") or "")[:140]} if sit else None,
        "week": (e.get("week") or {}).get("number"),
    }


def closing_line(league, eid):
    """(spread, total) of DraftKings' closing line from ESPN's core odds endpoint, for a started game the previous build
    never saw with a line. None if the feed has nothing."""
    try:
        d = fetch(CORE_ODDS.format(lg=LEAGUES[league]["core"], id=eid))
    except Exception:
        return None
    num = lambda v: float(v) if v not in (None, "") else None
    for it in d.get("items", []):
        name = (it.get("provider") or {}).get("name") or ""
        if name.startswith("DraftKings") and "Live" not in name:
            hc = ((it.get("homeTeamOdds") or {}).get("close") or {}).get("pointSpread") or {}
            tc = ((it.get("close") or {}).get("total") or {})
            sp, ou = num(hc.get("american")), num(tc.get("american"))
            if sp is None and ou is None:
                sp, ou = num(it.get("spread")), num(it.get("overUnder"))
            if sp is not None or ou is not None:
                return [sp, ou]
    return None


def previous_lines(html):
    """{event id: [spread, total]} from a slate file's data block, for games that had a line last build. Reads both this
    page's one-game-per-line block and the old board's."""
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
            sp, ou = g.get("spread", g.get("sp")), g.get("ou")
            if g.get("close"):
                sp, ou = g["close"]
            if sp is not None or ou is not None:
                out[str(g["id"])] = [sp, ou]
    return out


def artifact_copy(html, title):
    """The hosted artifact viewer supplies doctype, html, head and body, so keep only the title, the font link, the
    styles and the body content. The title stays fixed so the artifact keeps its name from week to week."""
    style = re.search(r"(<link[^>]*fonts\.googleapis[^>]*>\s*)?<style>.*?</style>", html, re.S)
    body = re.search(r"<body>(.*)</body>", html, re.S)
    if not (style and body):
        sys.exit("could not find <style> and <body> in the output; artifact copy not written")
    return f"<title>{title}</title>\n{style.group(0)}\n{body.group(1).strip()}\n"


def embed_logos(html, data):
    """Inline the team logos (logos.json beside the script, from the lab build) and the network logos as data URIs,
    for the artifact copies: the artifact viewer blocks outside images."""
    cache_path = os.path.join(HERE, "logos.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    logos = {}
    for lg, L in data["leagues"].items():
        for d in L["days"]:
            for g in d["games"]:
                for side in ("away", "home"):
                    k = lg + ":" + g[side]["logo"]
                    if cache.get(k):
                        logos[k] = cache[k]
    html = html.replace("/*LOGOS*/", "SLATE.logos = " + json.dumps(logos, separators=(",", ":")) + ";", 1)
    net_path = os.path.join(HERE, "netlogos.json")
    if os.path.exists(net_path):
        nets = {k: {"l": v["l"], "d": v.get("d", ""), "w": v.get("w"), "h": v.get("h")} for k, v in json.load(open(net_path)).items() if v.get("l")}
        html = re.sub(r"/\*NETLOGOS-START\*/.*?/\*NETLOGOS-END\*/", lambda m: "/*NETLOGOS-START*/\nconst NETLOGOS = " + json.dumps(nets, separators=(",", ":")) + ";\n/*NETLOGOS-END*/", html, count=1, flags=re.S)
    return html


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


def build_league(league, prev_lines, main_day, start, end):
    """Fetch one league's week. Returns (league data for the page, summary lines, week slug)."""
    L = LEAGUES[league]
    nfl = league == "nfl"
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    events, week, slug, provider = {}, None, None, None
    for d in dates:
        for g in L["groups"]:
            data = fetch(L["api"].format(d=d.strftime("%Y%m%d"), g=g))
            if (d == main_day or week is None) and g == L["groups"][0] and (data.get("week") or {}).get("number") is not None:
                week, slug = week_label(data, main_day, nfl)
            for e in data.get("events", []):
                if e.get("id") and e.get("competitions"):
                    events.setdefault(e["id"], e)
    if week is None:
        week, slug = week_label({}, main_day, nfl)
    if not events:
        raise LookupError(f"no games between {start} and {end}")

    days, unknown_conf, looked_up = {d: [] for d in dates}, {}, 0
    for e in events.values():
        c = e["competitions"][0]
        dt = datetime.fromisoformat(c["date"].replace("Z", "+00:00")).astimezone(ET)
        day = dt.date()
        if dt.hour < 6:  # a game after midnight ET belongs to the previous day's slate
            day -= timedelta(days=1)
        if day not in days:
            continue
        g = game(e, L, nfl)
        if not g:
            continue
        if not nfl:
            for comp in c["competitors"]:
                cid = str(comp["team"].get("conferenceId", ""))
                if cfb_conf_code(cid) == "OTHER":
                    unknown_conf.setdefault(cid or "none", set()).add(NAME_ALIAS.get(comp["team"].get("location", ""), comp["team"].get("location", "")))
        provider = provider or g["provider"]
        if g["state"] in ("in", "post") and g["spread"] is None and g["ou"] is None:
            # ESPN pulls the odds at kickoff: keep the last pregame number as the closing line, or ask the odds feed once
            line = prev_lines.get(g["id"])
            if (line is None or line[0] is None) and looked_up < 60:
                looked_up += 1
                line = closing_line(league, g["id"]) or line
            if line:
                g["close"] = line
        days[day].append(g)

    for d in days:
        days[d].sort(key=lambda g: (g["kick"], not g["tv"], g["carriers"][0] if g["carriers"] else "~", g["home"]["name"]))

    all_present = {n for gs in days.values() for g in gs for n in g["carriers"]}
    nets_present = {n for gs in days.values() for g in gs for n in g.pop("_tv", [])}   # every national TV channel, simulcasts included
    networks = [n for n in L["tv"] if n in nets_present] + sorted(n for n in nets_present if n not in L["tv"])
    order = networks + [n for n in L["stream"] if n in all_present] + sorted(n for n in all_present if n not in networks and n not in L["stream"])
    have = sorted(all_present) if DEFAULT_PACKAGES == "everything" else sorted({n for p in DEFAULT_PACKAGES for n in L["packages"].get(p, [])})
    out = {
        "name": L["name"], "week": week, "main": main_day.isoformat(), "mainLabel": main_day.strftime("%a, %b %-d"),
        "logo": L["logo"], "networks": networks, "order": order, "packages": L["packages"], "have": have,
        "confs": NFL_CONFS if nfl else cfb_conf_table(),
        "days": [{"label": d.strftime("%a %-m/%-d"), "date": d.isoformat(), "games": days[d]} for d in dates if days[d]],
    }
    pulled = datetime.now(ET).strftime("%a %-m/%-d, %-I:%M %p ET")
    any_line = any(g["spread"] is not None or g.get("close") for gs in days.values() for g in gs)
    lines_src = f"{provider or 'DraftKings'} via ESPN" if any_line else "unavailable"

    # summary, same shape as before (the Thursday health check reads the first line)
    rep = []
    total = sum(len(g) for g in days.values())
    rep.append(f"{L['name']} {week} ({total} games, pulled {pulled}, lines {lines_src})")
    for d in dates:
        gs = days[d]
        if gs:
            rep.append(f"  {d.strftime('%a %-m/%-d')}: {sum(g['tv'] for g in gs)} on TV, {sum(not g['tv'] for g in gs)} not")
    rk = lambda r: f"#{r} " if r else ""

    def line_of(g):
        sp, ou = (g["close"] if g.get("close") else (g["spread"], g["ou"]))
        if sp is None:
            return ""
        fav = g["home"] if sp < 0 else g["away"] if sp > 0 else None
        s = f"  {fav['ab']} -{abs(sp)}" if fav else "  PK"
        if ou is not None:
            s += f", o/u {ou}"
        return s
    def et_clock(g):
        dt = datetime.fromisoformat(g["kick"].replace("Z", "+00:00")).astimezone(ET)
        hh = dt.hour + (24 if dt.hour < 6 else 0)
        return f"{hh:02d}:{dt.minute:02d}"
    listed = [(d, g) for d in dates for g in days[d] if nfl or g["away"]["rank"] or g["home"]["rank"]]
    rep.append("  every game with its line:" if nfl else "  ranked matchups:")
    for d, g in listed:
        rep.append(f"    {d.strftime('%a')} {et_clock(g)} ET  {rk(g['away']['rank'])}{g['away']['name']} {'vs' if g['neutral'] else 'at'} {rk(g['home']['rank'])}{g['home']['name']}  [{g['carriers'][0] if g['carriers'] else 'no TV'}]{line_of(g)}")
    nolines = sum(1 for d in dates for g in days[d] if g["spread"] is None and not g.get("close") and g["tv"])
    rep.append(f"  TV games without a line: {nolines}")
    tbds = [(d, g) for d in dates for g in days[d] if g.get("tbd")]
    rep.append(f"  time TBD: {len(tbds)}" + (": " + "; ".join(f"{g['away']['name']} at {g['home']['name']}" for d, g in tbds) if tbds else ""))
    if unknown_conf:
        rep.append("  non-Division I or unknown conference ids: " + "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in unknown_conf.items()))
    return out, rep, slug, pulled, lines_src


def data_block(slate):
    """The data block, one game per line so the next build (and the old board's format) can read the lines back."""
    js = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    lines = ["const SLATE = {"]
    for k in ("v", "default", "pulled", "lines", "confIds", "nflDiv", "nameAlias"):
        lines.append(f"  {js(k)}: {js(slate[k])},")
    lines.append('  "leagues": {')
    keys = list(slate["leagues"])
    for li, lg in enumerate(keys):
        L = slate["leagues"][lg]
        lines.append(f"    {js(lg)}: {{")
        for k in ("name", "week", "main", "mainLabel", "logo", "networks", "order", "packages", "have", "confs"):
            lines.append(f"      {js(k)}: {js(L[k])},")
        lines.append('      "days": [')
        for i, d in enumerate(L["days"]):
            lines.append(f'        {{"label": {js(d["label"])}, "date": {js(d["date"])}, "games": [')
            for j, g in enumerate(d["games"]):
                lines.append("          " + js(g) + ("," if j < len(d["games"]) - 1 else ""))
            lines.append("        ]}" + ("," if i < len(L["days"]) - 1 else ""))
        lines.append("      ]")
        lines.append("    }" + ("," if li < len(keys) - 1 else ""))
    lines.append("  }")
    lines.append("};")
    lines.append("/*LOGOS*/")
    return "/*DATA-START*/\n/* Generated by build_slate.py. Do not hand-edit unless the script can't run;\n   see cfb-slate-handoff.md for the field reference. */\n" + "\n".join(lines) + "\n/*DATA-END*/"


LANDING = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Football Slate</title>
<style>html{color-scheme:dark}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#101722;color:#ece9e2;
font-family:"Barlow Condensed","Roboto Condensed","Arial Narrow","Helvetica Neue",Arial,sans-serif}.box{text-align:center;padding:24px}
h1{margin:0 0 18px;font-size:22px;letter-spacing:.06em;text-transform:uppercase}a{display:block;margin:10px auto;width:260px;padding:16px 0;border:1px solid rgba(255,255,255,.25);
border-radius:8px;color:#ece9e2;text-decoration:none;font-size:20px;font-weight:700;letter-spacing:.04em}a:hover{background:#1f2a3b}small{display:block;margin-top:14px;color:#9aa5b5;font-size:12px}</style></head>
<body><div class="box"><h1>Football slate</h1><a href="cfb/">College Football</a><a href="nfl/">NFL</a><small>Every game, by channel and kickoff, live scores and lines. One page; the College / NFL switch is inside.</small></div></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", choices=["both", "cfb", "nfl"], default="both", help="which league(s) to fetch (default both; the page carries what was fetched)")
    ap.add_argument("--main", help="grid day, YYYY-MM-DD (default: the coming Saturday for college, Sunday for the NFL); use with --league")
    ap.add_argument("--start", help="first day to include, YYYY-MM-DD (default: main - 2 college, main - 4 NFL)")
    ap.add_argument("--end", help="last day to include, YYYY-MM-DD (default: main + 2 college, main + 1 NFL)")
    ap.add_argument("--template", help="slate file to reuse the renderer from (default: newest cfb-slate-week*.html here)")
    ap.add_argument("--out", help="output file for a single-league build (default: cfb-slate-weekN.html or nfl-slate-weekN.html)")
    ap.add_argument("--artifact", default="", help="artifact-ready copies: default cfb-slate-artifact.html / nfl-slate-artifact.html; 'none' to skip")
    ap.add_argument("--embed", action="store_true", help="inline the team and network logos in the artifact copies (needs logos.json and netlogos.json beside the script)")
    ap.add_argument("--index", help="web root index file; writes cfb/index.html and nfl/index.html beside it and a landing page at it")
    ap.add_argument("--summary", help="summary file; writes cfb/summary.txt and nfl/summary.txt beside it too")
    args = ap.parse_args()
    leagues = ["cfb", "nfl"] if args.league == "both" else [args.league]

    template = args.template or newest("cfb-slate-*.html", exclude=("cfb-slate-artifact.html",))
    if not template:
        sys.exit("no cfb-slate-week*.html found to use as the template; pass --template")
    tpl = open(template, encoding="utf-8").read()
    if "/*DATA-START*/" not in tpl or "/*DATA-END*/" not in tpl:
        sys.exit("template is missing the /*DATA-START*/ ... /*DATA-END*/ markers")
    today = datetime.now(ET).date()
    web_root = os.path.dirname(args.index) if args.index else None

    slate = {"v": 2, "default": "cfb", "pulled": "", "lines": "", "leagues": {},
             "confIds": {**{k: v[0] for k, v in FBS_CONF.items()}, **{k: v[0] for k, v in FCS_CONF.items()}},
             "nflDiv": NFL_DIV, "nameAlias": NAME_ALIAS}
    reports, slugs, extra = [], {}, {}
    for league in leagues:
        L = LEAGUES[league]
        main_day = date.fromisoformat(args.main) if args.main else main_day_for(league, today)
        start = date.fromisoformat(args.start) if args.start else main_day - timedelta(days=L["before"])
        end = date.fromisoformat(args.end) if args.end else main_day + timedelta(days=L["after"])
        # last line seen for each game in the previous build (ESPN drops odds at kickoff): the web copy when there is
        # one, else the newest weekly file for that league, else the template
        web_prev = os.path.join(web_root, league, "index.html") if web_root is not None else None
        prev_src = web_prev if web_prev and os.path.exists(web_prev) else (newest(f"{L['file']}-*.html", exclude=(f"{L['file']}-artifact.html",)) or template)
        prev_lines = previous_lines(open(prev_src, encoding="utf-8").read()) if prev_src and os.path.exists(prev_src) else {}
        got = None
        for attempt in range(3):   # a window with no games (the gap before the season, a Sunday with none) rolls a week forward
            try:
                got = build_league(league, prev_lines, main_day, start, end)
                break
            except LookupError as ex:
                if args.main or attempt == 2:
                    reports.append([f"{L['name']}: {ex}"])
                    break
                main_day, start, end = (x + timedelta(days=7) for x in (main_day, start, end))
            except Exception as ex:   # one league failing shouldn't take the other down
                if len(leagues) == 1:
                    raise
                reports.append([f"{L['name']}: build failed ({ex.__class__.__name__}: {ex})"])
                break
        if got is None:
            continue
        data, rep, slug, pulled, lines_src = got
        slate["leagues"][league] = data
        slate["pulled"] = pulled
        if lines_src != "unavailable" or not slate["lines"]:
            slate["lines"] = lines_src
        slugs[league], extra[league] = slug, rep
    if not slate["leagues"]:
        text = "\n\n".join("\n".join(r) for r in reports)
        print(text)
        if args.summary:
            open(args.summary, "w", encoding="utf-8").write(text + "\n")
        sys.exit(1)

    def page(default):
        s = dict(slate, default=default)
        html, n = re.subn(r"/\*DATA-START\*/.*?/\*DATA-END\*/", lambda m: data_block(s), tpl, count=1, flags=re.S)
        L = LEAGUES[default]
        wk = slate["leagues"][default]["week"] if default in slate["leagues"] else ""
        return re.sub(r"<title>.*?</title>", f"<title>{L['title']} - {wk}</title>", html, count=1)

    for league in leagues:
        if league not in slate["leagues"]:
            continue
        L = LEAGUES[league]
        html = page(league)
        out = args.out if (args.out and len(leagues) == 1) else f"{L['file']}-{slugs[league]}.html"
        open(out, "w", encoding="utf-8").write(html)
        notes = [f"  weekly file written to {out}"]
        artifact = args.artifact or f"{L['file']}-artifact.html"
        if artifact != "none":
            open(artifact, "w", encoding="utf-8").write(artifact_copy(embed_logos(html, slate) if args.embed else html, L["title"]))
            notes.append(f"  artifact copy written to {artifact}" + (" (logos embedded)" if args.embed else ""))
        if args.index:
            folder = os.path.join(web_root, league)
            os.makedirs(folder, exist_ok=True)
            open(os.path.join(folder, "index.html"), "w", encoding="utf-8").write(html)
            notes.append(f"  web copy written to {os.path.join(folder, 'index.html')}")
        rep = extra[league]
        rep[1:1] = notes
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
