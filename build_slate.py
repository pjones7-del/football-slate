#!/usr/bin/env python3
"""
build_slate.py - build the weekly college football slate HTML from ESPN's public scoreboard API.

Pulls every FBS and FCS game from Thursday through Monday, works out the broadcast
(national TV vs streaming vs nothing), DraftKings spread and total, AP/CFP rank, team
colors and conference, then writes the data block into the slate template.

Usage:
  python3 build_slate.py                                   # this week's Thu-Mon, grid on Saturday
  python3 build_slate.py --main 2026-09-12                 # a specific Saturday
  python3 build_slate.py --template cfb-slate-week1.html --out cfb-slate-week2.html

Any existing slate file works as the template (default: the newest cfb-slate-*.html in
the current folder); only the block between /*DATA-START*/ and /*DATA-END*/ is replaced.
Also writes cfb-slate-artifact.html, the same page without the document skeleton and with a
fixed title, which is what gets published to the hosted artifact (pass --artifact none to skip).
--index index.html writes the full page under that name too (what the public web host serves);
--summary summary.txt saves the printed summary. The GitHub Actions workflow uses both.
Needs Python 3.9+ and internet access. No packages to install.
"""
import argparse, json, re, sys, urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
API = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?dates={d}&groups={g}&limit=300"
GROUPS = ("80", "81")  # 80 = FBS, 81 = FCS

# ESPN conferenceId -> code used by the renderer
FBS_CONF = {"8": "SEC", "5": "B1G", "1": "ACC", "4": "B12", "151": "AAC", "9": "PAC", "17": "MWC",
            "15": "MAC", "37": "SBC", "12": "CUSA", "18": "IND"}
FCS_CONF = {"20", "21", "22", "24", "25", "26", "27", "28", "29", "30", "31", "32", "40", "48", "177", "179"}

# grid row order for national TV channels; anything else national-TV is appended alphabetically
TV_ORDER = ["ABC", "CBS", "FOX", "NBC", "CW", "ESPN", "ESPN2", "TNT", "ESPNU", "FS1", "FS2", "USA", "ACCN", "SECN", "BTN", "CBSSN"]
NET_ALIAS = {"SEC Network": "SECN", "ACC Network": "ACCN", "USA Net": "USA", "USA Network": "USA",
             "Big Ten Network": "BTN", "CBS Sports Network": "CBSSN", "The CW": "CW", "CW Network": "CW",
             "TNT/HBO Max": "TNT", "truTV": "TRUTV", "ESPNews": "ESPNEWS", "ESPN News": "ESPNEWS", "NBC Sports Network": "NBCSN"}
# streaming services and regional feeds, in grid row order after the TV channels (ESPN+ last because it stacks deepest)
STREAM_ORDER = ["Peacock", "Disney+", "HBO Max", "Paramount+", "Fox One", "SECN+", "ACCNX", "MW+", "YouTube", "HBCU Go", "ESPN3", "ESPN+"]

# what each package carries, as ESPN's channel names above. Edit here when carriage changes.
LINEAR = ["ABC", "CBS", "FOX", "NBC", "CW", "ESPN", "ESPN2", "ESPNU", "ESPNEWS", "SECN", "ACCN", "BTN",
          "FS1", "FS2", "CBSSN", "USA", "TNT", "TBS", "TRUTV", "NBCSN"]
PACKAGES = {
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
DEFAULT_PACKAGES = "everything"   # what the file opens with: "everything" = every channel and service with a game that week,
                                  # or a list of PACKAGES names, e.g. ["YouTube TV", "Peacock", "ESPN Unlimited"]
NAME_ALIAS = {"Hawai'i": "Hawaii", "California": "Cal", "Pittsburgh": "Pitt", "Miami": "Miami (FL)",
              "Long Island University": "LIU", "Massachusetts": "UMass"}
STREAM_TYPES = {"Streaming", "Web"}


def fetch(d, g):
    req = urllib.request.Request(API.format(d=d, g=g))
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


def conf_code(cid):
    if cid in FBS_CONF:
        return FBS_CONF[cid]
    if cid in FCS_CONF:
        return "FCS"
    return "OTHER"


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


def artifact_copy(html, title="CFB Slate"):
    """The hosted artifact viewer supplies doctype, html, head and body, so keep only the title, the styles and the
    body content. The title stays fixed so the artifact keeps its name from week to week."""
    style = re.search(r"<style>.*?</style>", html, re.S)
    body = re.search(r"<body>(.*)</body>", html, re.S)
    if not (style and body):
        sys.exit("could not find <style> and <body> in the output; artifact copy not written")
    return f"<title>{title}</title>\n{style.group(0)}\n{body.group(1).strip()}\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--main", help="grid day, YYYY-MM-DD (default: this coming Saturday)")
    ap.add_argument("--start", help="first day to include, YYYY-MM-DD (default: main - 2)")
    ap.add_argument("--end", help="last day to include, YYYY-MM-DD (default: main + 2)")
    ap.add_argument("--template", help="slate file to reuse the renderer from (default: newest cfb-slate-*.html here)")
    ap.add_argument("--out", help="output file (default: cfb-slate-weekN.html)")
    ap.add_argument("--artifact", default="cfb-slate-artifact.html", help="artifact-ready copy to write (default: cfb-slate-artifact.html; 'none' to skip)")
    ap.add_argument("--index", help="also write the full page under this name (the web host serves index.html)")
    ap.add_argument("--summary", help="also write the printed summary to this file")
    args = ap.parse_args()

    today = datetime.now(ET).date()
    if args.main:
        main_day = date.fromisoformat(args.main)
    else:
        # Sunday and Monday still belong to the weekend just played (their games are on that slate);
        # Tuesday rolls forward to the coming Saturday. Matters for the daily rebuild.
        wd = today.weekday()  # Mon=0 .. Sun=6
        main_day = today - timedelta(days=1 if wd == 6 else 2) if wd in (6, 0) else today + timedelta(days=(5 - wd) % 7)
    start = date.fromisoformat(args.start) if args.start else main_day - timedelta(days=2)
    end = date.fromisoformat(args.end) if args.end else main_day + timedelta(days=2)
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    events, week_no, provider = {}, None, None
    for d in dates:
        for g in GROUPS:
            data = fetch(d.strftime("%Y%m%d"), g)
            if d == main_day and g == "80":
                week_no = (data.get("week") or {}).get("number")
            for e in data.get("events", []):
                if e.get("id") and e.get("competitions"):
                    events.setdefault(e["id"], e)

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
            name = NAME_ALIAS.get(t["location"], t["location"])
            cid = str(t.get("conferenceId", ""))
            code = conf_code(cid)
            if code == "OTHER":
                unknown_conf.setdefault(cid or "none", set()).add(name)
            if name not in teams:
                bg, fg = colors(t)
                teams[name] = {"l": label(name), "c": code, "ab": t.get("abbreviation", name), "bg": bg, "fg": fg}
                if t.get("logo") and t.get("id"):
                    teams[name]["lg"] = str(t["id"])   # ESPN team id; the page builds the logo URL from it
                alt_c = (t.get("alternateColor") or "").lower()
                if len(alt_c) == 6 and alt_c != bg.lstrip("#"):
                    teams[name]["_alt"] = "#" + alt_c
            rank = (comp.get("curatedRank") or {}).get("current", 99)
            side = {"name": name, "rank": rank if rank and rank <= 25 else 0}
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
                    (tv_nat if n in TV_ORDER else stream_nat).append(n)
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
            note = ", ".join(x for x in (adr.get("city"), adr.get("state")) if x) or v.get("fullName", "")

        game = {"id": e["id"], "n": chosen, "tv": bool(tv_nat), "k": f"{hh:02d}:{dt.minute:02d}", "a": away["name"], "h": home["name"],
                "ar": away["rank"], "hr": home["rank"], "neu": bool(c.get("neutralSite")), "note": note,
                "sp": sp, "ou": ou}
        if alt:
            game["alt"] = alt
        # build-time snapshot of a started game: state (in/post), scores, clock text. The page refreshes these live.
        live = live_state(e, c)
        if live:
            game["live"] = live
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
    networks = [n for n in TV_ORDER if n in nets_present] + sorted(n for n in nets_present if n not in TV_ORDER)
    order = networks + [n for n in STREAM_ORDER if n in all_present] + sorted(n for n in all_present if n not in networks and n not in STREAM_ORDER)
    have = sorted(all_present) if DEFAULT_PACKAGES == "everything" else sorted({n for p in DEFAULT_PACKAGES for n in PACKAGES.get(p, [])})
    pulled = datetime.now(ET).strftime("%a %-m/%-d, %-I:%M %p ET")
    slate = {
        "week": f"Week {week_no}" if week_no is not None else main_day.strftime("Week of %b %-d"),
        "main": main_day.isoformat(),
        "mainLabel": main_day.strftime("%a, %b %-d"),
        "pulled": pulled,
        "lines": f"{provider} via ESPN" if provider else "unavailable",
        "networks": networks,
        "order": order,
        "packages": PACKAGES,
        "have": have,
        "days": [{"label": d.strftime("%a %-m/%-d"), "date": d.isoformat(), "games": days[d]} for d in dates if days[d]],
    }

    def js(o):
        return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    lines = ["const SLATE = {"]
    for k in ("week", "main", "mainLabel", "pulled", "lines", "networks", "order", "packages", "have"):
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

    template = args.template
    if not template:
        import glob, os
        # skip the artifact copy from an earlier run: it has no <body>, so it can't serve as the template
        cands = sorted((f for f in glob.glob("cfb-slate-*.html") if f not in (args.artifact, "cfb-slate-artifact.html")),
                       key=os.path.getmtime)
        if not cands:
            sys.exit("no cfb-slate-*.html found to use as the template; pass --template")
        template = cands[-1]
    tpl = open(template, encoding="utf-8").read()
    out_html, n = re.subn(r"/\*DATA-START\*/.*?/\*DATA-END\*/", lambda m: block, tpl, count=1, flags=re.S)
    if n != 1:
        sys.exit("template is missing the /*DATA-START*/ ... /*DATA-END*/ markers")
    out_html = re.sub(r"<title>.*?</title>", f"<title>CFB Slate - {slate['week']}</title>", out_html, count=1)
    out = args.out or f"cfb-slate-week{week_no}.html"
    open(out, "w", encoding="utf-8").write(out_html)
    if args.artifact != "none":
        open(args.artifact, "w", encoding="utf-8").write(artifact_copy(out_html))
    if args.index:
        open(args.index, "w", encoding="utf-8").write(out_html)

    # summary
    rep = []
    total = sum(len(g) for g in days.values())
    rep.append(f"{slate['week']} slate written to {out} ({total} games, pulled {pulled}, lines {slate['lines']})")
    if args.artifact != "none":
        rep.append(f"  artifact copy written to {args.artifact}")
    if args.index:
        rep.append(f"  web copy written to {args.index}")
    for d in dates:
        gs = days[d]
        if gs:
            rep.append(f"  {d.strftime('%a %-m/%-d')}: {sum(g['tv'] for g in gs)} on TV, {sum(not g['tv'] for g in gs)} not")
    ranked = [(d, g) for d in dates for g in days[d] if g["ar"] or g["hr"]]
    rep.append("  ranked matchups:")
    for d, g in ranked:
        rk = lambda r: f"#{r} " if r else ""
        line = ""
        if g["sp"] is not None:
            fav = g["h"] if g["sp"] < 0 else g["a"] if g["sp"] > 0 else None
            line = f"  {teams[fav]['ab']} -{abs(g['sp'])}" if fav else "  PK"
            if g["ou"] is not None:
                line += f", o/u {g['ou']}"
        rep.append(f"    {d.strftime('%a')} {g['k']} ET  {rk(g['ar'])}{g['a']} {'vs' if g['neu'] else 'at'} {rk(g['hr'])}{g['h']}  [{g['n'] or 'no TV'}]{line}")
    nolines = sum(1 for d in dates for g in days[d] if g["sp"] is None and g["tv"])
    rep.append(f"  TV games without a line: {nolines}")
    tbds = [(d, g) for d in dates for g in days[d] if g.get("tbd")]
    rep.append(f"  time TBD: {len(tbds)}" + (": " + "; ".join(f"{g['a']} at {g['h']}" for d, g in tbds) if tbds else ""))
    if unknown_conf:
        rep.append("  non-Division I or unknown conference ids: " + "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in unknown_conf.items()))
    print("\n".join(rep))
    if args.summary:
        open(args.summary, "w", encoding="utf-8").write("\n".join(rep) + "\n")


if __name__ == "__main__":
    main()
