#!/usr/bin/env python3
"""
fetch_macro.py - Macro Data Layer engine for the `macro-monitor` skill.

Pulls structured macro / economic time-series from FRED's KEYLESS CSV endpoint
    https://fred.stlouisfed.org/graph/fredgraph.csv?id=SERIES_ID
computes the current level, changes (1m / 3m / 6m / 12m), a z-score and
percentile versus a trailing window, and a trend label - then prints ONE
compact, token-efficient summary table. The raw CSV never needs to enter the
conversation (this is the cheap path, mirroring market-regime-analyzer).

Design notes
------------
* Stdlib only (urllib, csv, statistics, argparse). PyYAML is used ONLY if a
  --series-file is supplied AND the module is importable; otherwise the
  embedded DEFAULT_PANEL is used, so the script always runs out of the box.
* No API key required. FRED's fredgraph.csv endpoint is public.
* Missing observations in FRED CSV are the literal ".", which are skipped.
* Caching: with --cache DIR the script saves each series and, on a re-run,
  fetches only observations since the last cached date (FRED `cosd=` param),
  merges, and re-saves -> near-zero cost for repeat questions in a chat.
* Offline fallback: --input SERIES=path.csv uses a pre-downloaded CSV (for the
  web_fetch path when the container has no direct network).

Usage
-----
  python fetch_macro.py                          # full default panel, live
  python fetch_macro.py --series DGS10,DFII10    # a specific subset
  python fetch_macro.py --cache ./macro_cache    # incremental cache
  python fetch_macro.py --quadrant               # add growth/inflation read
  python fetch_macro.py --json out.json          # also write machine-readable
  python fetch_macro.py --input DGS10=dgs10.csv  # offline: use saved CSV
"""

import argparse
import csv
import io
import json
import os
import statistics
import sys
import urllib.request
from datetime import datetime, timedelta

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
# FRED's WAF only accepts recognized clients (curl/*, the default Python-urllib/*,
# real browsers). A spoofed "Mozilla/..." OR an unknown custom UA gets silently
# dropped (read-timeout). So we send NO custom User-Agent and let urllib use its
# default "Python-urllib/x", which FRED accepts. See fetch_csv().
UA = ""

# --- The default macro panel -------------------------------------------------
# Each entry: id, label, theme, freq, transform, growth, inflation, note
#   transform : None | "yoy"  (yoy turns an index level into a YoY % headline)
#   growth    : +1 / -1 / 0   contribution direction to the GROWTH axis
#   inflation : +1 / -1 / 0   contribution direction to the INFLATION axis
# Themes are printed in this order:
THEME_ORDER = [
    "Rates & Curve",
    "Inflation & Real Yields",
    "Growth & Labor",
    "Dollar & Financial Conditions",
    "Thailand",
]

DEFAULT_PANEL = [
    # id,           label,                        theme,                            freq,      transform, g,  i,  note
    ("DGS10",       "10Y Treasury Yield",         "Rates & Curve",                  "daily",   None,       0,  0,  ""),
    ("DGS2",        "2Y Treasury Yield",          "Rates & Curve",                  "daily",   None,       0,  0,  ""),
    ("DGS30",       "30Y Treasury Yield",         "Rates & Curve",                  "daily",   None,       0,  0,  "long-end / term premium"),
    ("T10Y2Y",      "Yield Curve 10Y-2Y",         "Rates & Curve",                  "daily",   None,       0,  0,  "negative = inverted"),
    ("T10Y3M",      "Yield Curve 10Y-3M",         "Rates & Curve",                  "daily",   None,       0,  0,  "Fed's recession gauge"),
    ("DFF",         "Fed Funds (effective)",      "Rates & Curve",                  "daily",   None,       0,  0,  ""),
    ("DFII10",      "10Y Real Yield (TIPS)",      "Inflation & Real Yields",        "daily",   None,       0,  0,  "key gold driver (up = gold headwind)"),
    ("T10YIE",      "10Y Breakeven Inflation",    "Inflation & Real Yields",        "daily",   None,       0, +1,  "market-implied inflation"),
    ("T5YIFR",      "5y5f Forward Inflation",     "Inflation & Real Yields",        "daily",   None,       0, +1,  "long-run inflation expectation"),
    ("CPIAUCSL",    "CPI (YoY)",                  "Inflation & Real Yields",        "monthly", "yoy",      0, +1,  "headline CPI, YoY%"),
    ("PCEPILFE",    "Core PCE (YoY)",             "Inflation & Real Yields",        "monthly", "yoy",      0, +1,  "Fed's preferred gauge"),
    ("DCOILWTICO",  "WTI Crude Oil",              "Inflation & Real Yields",        "daily",   None,       0,  0,  "energy = inflation impulse (noisy)"),
    ("UNRATE",      "Unemployment Rate",          "Growth & Labor",                 "monthly", None,      -1,  0,  "up = growth weakening"),
    ("ICSA",        "Initial Jobless Claims",     "Growth & Labor",                 "weekly",  None,      -1,  0,  "timely labor signal; up = weakening"),
    ("INDPRO",      "Industrial Production (YoY)","Growth & Labor",                  "monthly", "yoy",     +1,  0,  ""),
    ("DTWEXBGS",    "USD Broad Index",            "Dollar & Financial Conditions",  "daily",   None,       0,  0,  "strong USD = global tightening"),
    ("NFCI",        "Financial Conditions (CFNCI)","Dollar & Financial Conditions", "weekly",  None,       0,  0,  ">0 = tighter than average"),
    ("BAMLH0A0HYM2","High-Yield Credit Spread",   "Dollar & Financial Conditions",  "daily",   None,       0,  0,  "widening = risk stress"),
    ("VIXCLS",      "VIX",                        "Dollar & Financial Conditions",  "daily",   None,       0,  0,  "equity-vol / fear gauge"),
    ("WALCL",       "Fed Balance Sheet",          "Dollar & Financial Conditions",  "weekly",  None,       0,  0,  "QT (falling) / QE (rising) liquidity"),
    ("DEXTHUS",     "USDTHB (THB per USD)",       "Thailand",                       "daily",   None,       0,  0,  "up = THB weaker vs USD"),
    ("RBTHBIS",     "THB Real Effective FX (BIS)","Thailand",                       "monthly", None,       0,  0,  ">100 = THB strong vs own history"),
]

# horizon (calendar days) used for the "trend" that feeds the quadrant read
QUADRANT_HORIZON_DAYS = 95  # ~3 months


def panel_from_default():
    keys = ("id", "label", "theme", "freq", "transform", "growth", "inflation", "note")
    return [dict(zip(keys, row)) for row in DEFAULT_PANEL]


def load_panel(series_file):
    """Load a panel from a YAML/JSON file if possible; else default panel."""
    if not series_file:
        return panel_from_default()
    if not os.path.exists(series_file):
        sys.stderr.write("series-file not found, using default panel: %s\n" % series_file)
        return panel_from_default()
    text = open(series_file, "r", encoding="utf-8").read()
    data = None
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
    except Exception:
        try:
            data = json.loads(text)
        except Exception:
            sys.stderr.write("could not parse series-file (need PyYAML or JSON); using default\n")
            return panel_from_default()
    rows = data.get("series", data) if isinstance(data, dict) else data
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "label": r.get("label", r["id"]),
            "theme": r.get("theme", "Other"),
            "freq": r.get("freq", "daily"),
            "transform": r.get("transform"),
            "growth": int(r.get("growth", 0)),
            "inflation": int(r.get("inflation", 0)),
            "note": r.get("note", ""),
        })
    return out


# --- fetching ----------------------------------------------------------------
def fetch_csv(series_id, start=None):
    url = FRED_CSV.format(id=series_id)
    if start:
        url += "&cosd=" + start
    # send a custom UA only if explicitly set; default (no header) lets urllib
    # use "Python-urllib/x", which FRED accepts. A custom UA can get WAF-dropped.
    req = urllib.request.Request(url, headers=({"User-Agent": UA} if UA else {}))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_csv(text):
    """Return sorted [(date_str, float)], skipping '.' missing values."""
    obs = []
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return obs
    # header is either DATE,<id> or observation_date,<id>
    for row in rows[1:]:
        if len(row) < 2:
            continue
        d, v = row[0].strip(), row[1].strip()
        if not d or v in (".", "", "NaN"):
            continue
        try:
            obs.append((d, float(v)))
        except ValueError:
            continue
    obs.sort(key=lambda x: x[0])
    return obs


def get_series(series_id, cache_dir=None, input_path=None):
    """Return observations, using offline input or incremental cache when set."""
    if input_path:
        return parse_csv(open(input_path, "r", encoding="utf-8").read())

    if not cache_dir:
        return parse_csv(fetch_csv(series_id))

    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, series_id + ".json")
    cached = []
    if os.path.exists(cache_file):
        try:
            cached = [tuple(x) for x in json.load(open(cache_file, "r", encoding="utf-8"))]
        except Exception:
            cached = []
    try:
        if cached:
            last = cached[-1][0]
            start = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
            fresh = parse_csv(fetch_csv(series_id, start=start))
            merged = {d: v for d, v in cached}
            for d, v in fresh:
                merged[d] = v
            obs = sorted(merged.items())
        else:
            obs = parse_csv(fetch_csv(series_id))
        json.dump(obs, open(cache_file, "w", encoding="utf-8"))
        return [tuple(x) for x in obs]
    except Exception as e:
        if cached:
            sys.stderr.write("[%s] network failed, using cache (stale): %s\n" % (series_id, e))
            return cached
        raise


# --- transforms & stats ------------------------------------------------------
def to_yoy(obs):
    """Convert an index level series to YoY % (date, yoy_pct)."""
    out = []
    for i, (d, v) in enumerate(obs):
        target = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
        prev = value_on_or_before(obs[:i + 1], target)
        if prev and prev != 0:
            out.append((d, (v / prev - 1.0) * 100.0))
    return out


def value_on_or_before(obs, date_str):
    """Latest value with date <= date_str (obs sorted ascending)."""
    chosen = None
    for d, v in obs:
        if d <= date_str:
            chosen = v
        else:
            break
    return chosen


def change_over(obs, days):
    """(level_change, pct_change) over `days` calendar days from the last obs."""
    if len(obs) < 2:
        return None, None
    last_d, last_v = obs[-1]
    target = (datetime.strptime(last_d, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    prev = value_on_or_before(obs[:-1], target)
    if prev is None:
        return None, None
    lvl = last_v - prev
    pct = (last_v / prev - 1.0) * 100.0 if prev != 0 else None
    return lvl, pct


def zscore_pct(obs, window_years):
    """z-score and percentile of the latest value vs a trailing window."""
    if len(obs) < 8:
        return None, None
    last_d, last_v = obs[-1]
    cut = (datetime.strptime(last_d, "%Y-%m-%d") - timedelta(days=int(window_years * 365))).strftime("%Y-%m-%d")
    window = [v for d, v in obs if d >= cut]
    if len(window) < 8:
        window = [v for _, v in obs]
    try:
        mean = statistics.fmean(window)
        sd = statistics.pstdev(window)
    except Exception:
        return None, None
    z = (last_v - mean) / sd if sd else 0.0
    below = sum(1 for v in window if v <= last_v)
    pctile = 100.0 * below / len(window)
    return z, pctile


def trend_label(chg3m_level):
    if chg3m_level is None:
        return "n/a"
    if chg3m_level > 0:
        return "rising"
    if chg3m_level < 0:
        return "falling"
    return "flat"


def compute(entry, obs, window_years):
    if entry.get("transform") == "yoy":
        obs = to_yoy(obs)
    if not obs:
        return None
    last_d, last_v = obs[-1]
    l1, p1 = change_over(obs, 30)
    l3, p3 = change_over(obs, 95)
    l6, p6 = change_over(obs, 185)
    l12, p12 = change_over(obs, 365)
    z, pctile = zscore_pct(obs, window_years)
    return {
        "id": entry["id"], "label": entry["label"], "theme": entry["theme"],
        "note": entry["note"], "transform": entry.get("transform"),
        "growth": entry["growth"], "inflation": entry["inflation"],
        "asof": last_d, "value": last_v,
        "chg_1m": l1, "chg_3m": l3, "chg_6m": l6, "chg_12m": l12,
        "z": z, "pctile": pctile, "trend": trend_label(l3),
        "n": len(obs),
    }


# --- growth / inflation quadrant (their four-quadrant framework) -------------
def quadrant(rows):
    def axis(attr):
        votes = []
        for r in rows:
            d = r[attr]
            if not d or r["chg_3m"] is None:
                continue
            s = 1 if r["chg_3m"] > 0 else (-1 if r["chg_3m"] < 0 else 0)
            votes.append(s * d)
        if not votes:
            return 0.0, 0, []
        return statistics.fmean(votes), len(votes), votes

    g, gn, _ = axis("growth")
    inf, infn, _ = axis("inflation")
    growth_up = g > 0.05
    growth_dn = g < -0.05
    infl_up = inf > 0.05

    if growth_up and infl_up:
        name = "Reflation / Overheating"
        favors = "commodities, gold, value/cyclicals, TIPS; bonds & long-duration suffer"
    elif growth_up and not infl_up:
        name = "Goldilocks (growth, cooling inflation)"
        favors = "equities (esp. growth/quality); the friendliest quadrant for risk assets"
    elif growth_dn and infl_up:
        name = "Stagflation"
        favors = "gold & commodities; cash; both stocks AND nominal bonds struggle"
    elif growth_dn and not infl_up:
        name = "Deflation / Slowdown"
        favors = "long-duration Treasuries & USD; risk assets vulnerable"
    else:
        name = "Mixed / Transition"
        favors = "no dominant axis - reduce factor bets, stay balanced (all-weather)"

    conf = "low"
    if gn >= 2 and infn >= 2 and (abs(g) > 0.3 or abs(inf) > 0.3):
        conf = "medium"
    if gn >= 3 and infn >= 3 and abs(g) > 0.5 and abs(inf) > 0.5:
        conf = "high"

    return {
        "growth_score": round(g, 2), "growth_n": gn,
        "inflation_score": round(inf, 2), "inflation_n": infn,
        "quadrant": name, "favors": favors, "confidence": conf,
    }


# --- output ------------------------------------------------------------------
def fmt(x, nd=2):
    return ("%+.{}f".format(nd) % x) if isinstance(x, float) else "  n/a"


def fmt_plain(x, nd=2):
    return ("%.{}f".format(nd) % x) if isinstance(x, float) else " n/a"


def print_table(rows, args):
    print("MACRO MONITOR  -  source: FRED (keyless CSV)  -  generated: %s"
          % datetime.utcnow().strftime("%Y-%m-%d %H:%MZ"))
    print("z = z-score vs %gy window; pct = percentile in window; chg = level change\n" % args.window_years)
    by_theme = {}
    for r in rows:
        by_theme.setdefault(r["theme"], []).append(r)
    order = [t for t in THEME_ORDER if t in by_theme] + [t for t in by_theme if t not in THEME_ORDER]
    head = "%-26s %10s %8s %8s %8s %7s %6s  %-8s %s" % (
        "series (id)", "as of", "value", "Δ3m", "Δ12m", "z", "pct", "trend", "note")
    for theme in order:
        print("== %s ==" % theme)
        print(head)
        for r in by_theme[theme]:
            unit = "%" if r["transform"] == "yoy" else ""
            print("%-26s %10s %7s%1s %8s %8s %7s %5s%%  %-8s %s" % (
                ("%s (%s)" % (r["label"], r["id"]))[:26],
                r["asof"],
                fmt_plain(r["value"]), unit,
                fmt(r["chg_3m"]),
                fmt(r["chg_12m"]),
                fmt(r["z"], 1) if r["z"] is not None else " n/a",
                ("%2.0f" % r["pctile"]) if r["pctile"] is not None else "n/a",
                r["trend"],
                r["note"],
            ))
        print("")


# --- growth/inflation trail (2-axis movement over time) ----------------------
def _pdate(d):
    return datetime.strptime(d, "%Y-%m-%d")


def _val_on_or_before(obs, target):
    chosen = None
    for d, v in obs:
        if _pdate(d) <= target:
            chosen = v
        else:
            break
    return chosen


def metric_asof(entry, obs, asof):
    """Headline metric as of a date: YoY% for transform=yoy series, else the level."""
    if entry.get("transform") == "yoy":
        a = _val_on_or_before(obs, asof)
        b = _val_on_or_before(obs, asof - timedelta(days=365))
        if a is not None and b not in (None, 0):
            return a / b - 1.0
        return None
    return _val_on_or_before(obs, asof)


def axis_asof(panel, series_obs, asof, which):
    """Growth or inflation axis score as of a date (same 3m-direction vote as quadrant)."""
    votes = []
    for e in panel:
        d = e.get(which, 0)
        if not d:
            continue
        obs = series_obs.get(e["id"])
        if not obs:
            continue
        m0 = metric_asof(e, obs, asof)
        m3 = metric_asof(e, obs, asof - timedelta(days=95))
        if m0 is None or m3 is None:
            continue
        votes.append((1 if m0 > m3 else (-1 if m0 < m3 else 0)) * d)
    return (statistics.fmean(votes) if votes else 0.0), len(votes)


def compute_trail(panel, series_obs, quad, months):
    """Monthly (growth, inflation) points over the last N months; tip = live quadrant."""
    latest = None
    for obs in series_obs.values():
        if obs:
            dt = _pdate(obs[-1][0])
            if latest is None or dt > latest:
                latest = dt
    if latest is None:
        return []
    pts = []
    for k in range(months, 0, -1):
        asof = latest - timedelta(days=30 * k)
        g, _ = axis_asof(panel, series_obs, asof, "growth")
        i, _ = axis_asof(panel, series_obs, asof, "inflation")
        pts.append({"date": asof.strftime("%Y-%m-%d"), "g": round(g, 3), "i": round(i, 3)})
    pts.append({"date": latest.strftime("%Y-%m-%d"),
                "g": quad["growth_score"], "i": quad["inflation_score"]})
    return pts


def build_sparks(panel, series_obs, months):
    """Monthly value history per series (YoY% for yoy series) for sparkline charts."""
    latest = None
    for obs in series_obs.values():
        if obs:
            dt = _pdate(obs[-1][0])
            if latest is None or dt > latest:
                latest = dt
    if latest is None:
        return {}
    dates = [latest - timedelta(days=30 * k) for k in range(months, -1, -1)]
    out = {"dates": [d.strftime("%Y-%m-%d") for d in dates], "series": {}}
    for e in panel:
        obs = series_obs.get(e["id"])
        if not obs:
            continue
        yoy = e.get("transform") == "yoy"
        vals = []
        for dt in dates:
            m = metric_asof(e, obs, dt)
            vals.append(round(m * 100 if yoy else m, 3) if m is not None else None)
        out["series"][e["id"]] = {"label": e["label"], "unit": "%" if yoy else "", "values": vals}
    return out


def main():
    ap = argparse.ArgumentParser(description="Macro Data Layer engine (FRED keyless).")
    ap.add_argument("--series", help="comma-separated FRED ids to restrict the panel to")
    ap.add_argument("--series-file", help="YAML/JSON panel file (overrides default)")
    ap.add_argument("--cache", help="cache dir for incremental fetch")
    ap.add_argument("--input", action="append", default=[],
                    help="offline: SERIES=path.csv (repeatable)")
    ap.add_argument("--window-years", type=float, default=5.0,
                    help="trailing window for z-score / percentile (default 5)")
    ap.add_argument("--quadrant", action="store_true",
                    help="print the growth/inflation four-quadrant read")
    ap.add_argument("--json", help="also write machine-readable JSON to this path")
    ap.add_argument("--trail-months", type=int, default=0,
                    help="also compute the growth/inflation trail over the last N months "
                         "(feeds the 2-axis quadrant movement chart)")
    args = ap.parse_args()

    panel = load_panel(args.series_file)
    if args.series:
        wanted = [s.strip() for s in args.series.split(",") if s.strip()]
        bypanel = {e["id"]: e for e in panel}
        panel = [bypanel.get(s, {"id": s, "label": s, "theme": "Other",
                                 "freq": "daily", "transform": None,
                                 "growth": 0, "inflation": 0, "note": ""})
                 for s in wanted]

    inputs = {}
    for spec in args.input:
        if "=" in spec:
            k, v = spec.split("=", 1)
            inputs[k.strip()] = v.strip()

    rows, errors, series_obs = [], [], {}
    for entry in panel:
        sid = entry["id"]
        try:
            obs = get_series(sid, cache_dir=args.cache, input_path=inputs.get(sid))
            series_obs[sid] = obs
            r = compute(entry, obs, args.window_years)
            if r:
                rows.append(r)
            else:
                errors.append((sid, "no observations"))
        except Exception as e:
            errors.append((sid, str(e)))

    if not rows:
        print("ERROR: no series fetched. Network blocked? Use --input SERIES=file.csv "
              "after web_fetch of https://fred.stlouisfed.org/graph/fredgraph.csv?id=ID")
        for sid, e in errors:
            print("  - %s: %s" % (sid, e))
        sys.exit(2)

    print_table(rows, args)

    quad = None
    if args.quadrant:
        quad = quadrant(rows)
        print("== Growth / Inflation Backdrop (proxy of the four-quadrant framework) ==")
        print("  Growth axis    : %+.2f  (n=%d)" % (quad["growth_score"], quad["growth_n"]))
        print("  Inflation axis : %+.2f  (n=%d)" % (quad["inflation_score"], quad["inflation_n"]))
        print("  -> Quadrant    : %s   [confidence: %s]" % (quad["quadrant"], quad["confidence"]))
        print("     Historically favors: %s" % quad["favors"])
        print("  (proxy only - trend signs of a few series; not a forecast)\n")

    trail, sparks = None, None
    if args.trail_months > 0:
        if quad is None:
            quad = quadrant(rows)
        trail = compute_trail(panel, series_obs, quad, args.trail_months)
        sparks = build_sparks(panel, series_obs, args.trail_months)
        print("== Growth/Inflation Trail (2-axis movement, last %d months) ==" % args.trail_months)
        print("  %-12s %8s %10s" % ("as-of", "growth", "inflation"))
        for p in trail:
            print("  %-12s %+8.2f %+10.2f" % (p["date"], p["g"], p["i"]))
        print("  (sparkline series for %d indicators added to --json)\n" % len(sparks.get("series", {})))

    if errors:
        print("notes: %d series unavailable -> %s" % (
            len(errors), ", ".join("%s (%s)" % (s, e[:40]) for s, e in errors)))

    if args.json:
        json.dump({"generated_utc": datetime.utcnow().isoformat(),
                   "window_years": args.window_years,
                   "rows": rows, "quadrant": quad, "trail": trail,
                   "sparks": sparks, "errors": errors},
                  open(args.json, "w", encoding="utf-8"), indent=2)
        print("wrote JSON: %s" % args.json)


if __name__ == "__main__":
    main()
