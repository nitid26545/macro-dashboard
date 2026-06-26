#!/usr/bin/env python3
"""
build_dashboard.py - generate a self-contained macro dashboard HTML from LIVE FRED data.

Reuses fetch_macro.py (same folder) for fetching + computing. Pure stdlib.
Reads a template HTML (default ../assets/macro_dashboard.html), replaces the block
between the /* DATA_START */ and /* DATA_END */ markers with fresh data, and writes
--out (default index.html). Run by the daily GitHub Action, or locally:

    python build_dashboard.py --out index.html
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_macro as fm  # noqa: E402

UNIT = {"DGS10": "%", "DGS2": "%", "DGS30": "%", "T10Y2Y": "", "T10Y3M": "", "DFF": "%",
        "DFII10": "%", "T10YIE": "%", "T5YIFR": "%", "CPIAUCSL": "%", "PCEPILFE": "%",
        "DCOILWTICO": "", "UNRATE": "%", "ICSA": "", "INDPRO": "%", "DTWEXBGS": "",
        "NFCI": "", "BAMLH0A0HYM2": "%", "VIXCLS": "", "WALCL": "", "DEXTHUS": "", "RBTHBIS": ""}
THEME_DISP = {"Rates & Curve": "Rates", "Inflation & Real Yields": "Inflation",
              "Growth & Labor": "Growth", "Dollar & Financial Conditions": "Dollar/FCI",
              "Thailand": "Thailand"}
SPARKS = [("DGS10", "10Y yield"), ("DFII10", "10Y real yield"), ("T10YIE", "Breakeven 10Y"),
          ("CPIAUCSL", "CPI YoY"), ("UNRATE", "Unemployment"), ("DTWEXBGS", "USD broad"),
          ("BAMLH0A0HYM2", "HY spread"), ("VIXCLS", "VIX"), ("DEXTHUS", "USDTHB")]
CONF_TH = {"low": "ต่ำ", "medium": "กลาง", "high": "สูง"}
MON_TH = {1: "ม.ค.", 2: "ก.พ.", 3: "มี.ค.", 4: "เม.ย.", 5: "พ.ค.", 6: "มิ.ย.",
          7: "ก.ค.", 8: "ส.ค.", 9: "ก.ย.", 10: "ต.ค.", 11: "พ.ย.", 12: "ธ.ค."}


def th_short(dstr):
    d = fm._pdate(dstr)
    return "%s%02d" % (MON_TH[d.month], d.year % 100)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Build the macro dashboard HTML from live FRED data.")
    ap.add_argument("--template", default=os.path.join(here, "..", "assets", "macro_dashboard.html"))
    ap.add_argument("--out", default="index.html")
    ap.add_argument("--window-years", type=float, default=5.0)
    ap.add_argument("--trail-months", type=int, default=12)
    args = ap.parse_args()

    panel = fm.panel_from_default()
    # bound history to ~8y: enough for a 5y z-score window + YoY, far faster than
    # pulling each series' full history (some go back to the 1950s-60s).
    start = (datetime.utcnow() - timedelta(days=365 * 8)).strftime("%Y-%m-%d")
    series_obs, comp_rows, errors = {}, [], []
    for e in panel:
        try:
            obs = fm.parse_csv(fm.fetch_csv(e["id"], start=start))
            series_obs[e["id"]] = obs
            r = fm.compute(e, obs, args.window_years)
            if not r:
                errors.append(e["id"])
                continue
            prev = None
            if len(obs) >= 2:
                m = fm.metric_asof(e, obs, fm._pdate(obs[-2][0]))
                if m is not None:
                    prev = m * 100.0 if e.get("transform") == "yoy" else m
            comp_rows.append({"e": e, "r": r, "prev": prev})
        except Exception as ex:
            errors.append("%s(%s)" % (e["id"], ex))

    if not comp_rows:
        sys.exit("ERROR: no series fetched (network?). " + ", ".join(errors))

    quad = fm.quadrant([c["r"] for c in comp_rows])
    trail = fm.compute_trail(panel, series_obs, quad, args.trail_months)
    sparks_all = fm.build_sparks(panel, series_obs, 36)

    drows = []
    for c in comp_rows:
        e, r, prev = c["e"], c["r"], c["prev"]
        val = r["value"]
        dprev = (val - prev) if prev is not None else 0.0
        dyoy = r["chg_12m"] if r["chg_12m"] is not None else 0.0
        drows.append([THEME_DISP.get(e["theme"], e["theme"]), e["label"], UNIT.get(e["id"], ""),
                      round(val, 2), round(prev if prev is not None else val, 2),
                      round(dprev, 2), round(dyoy, 2), round(r["z"], 2) if r["z"] is not None else 0.0])

    sp = []
    for sid, lab in SPARKS:
        s = sparks_all.get("series", {}).get(sid)
        if not s:
            continue
        vals = [v for v in s["values"] if v is not None]
        if len(vals) >= 2:
            sp.append([lab, s["unit"], vals])

    latest = max((fm._pdate(o[-1][0]) for o in series_obs.values() if o), default=datetime.utcnow())
    data = {
        "asof": "ล่าสุด %s (บางตัวรายเดือน ล่าช้าหลายสัปดาห์)" % latest.strftime("%Y-%m-%d"),
        "quad": {"name": quad["quadrant"], "g": quad["growth_score"], "i": quad["inflation_score"],
                 "conf": CONF_TH.get(quad["confidence"], quad["confidence"])},
        "trail": [{"d": ("now" if i == len(trail) - 1 else th_short(p["date"])),
                   "g": p["g"], "i": p["i"]} for i, p in enumerate(trail)],
        "rows": drows,
        "sparks": sp,
    }
    gen = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    tpl = open(args.template, encoding="utf-8").read()
    if "/* DATA_START */" not in tpl or "/* DATA_END */" not in tpl:
        sys.exit("template missing DATA_START/DATA_END markers: " + args.template)
    block = "/* DATA_START */\nconst GEN=%s;\nconst D=%s;\n/* DATA_END */" % (
        json.dumps(gen, ensure_ascii=False), json.dumps(data, ensure_ascii=False))
    out_html = re.sub(r"/\* DATA_START \*/.*?/\* DATA_END \*/", lambda m: block, tpl, flags=re.S)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out_html)
    print("wrote %s | %d series, %d trail pts, %d sparks | quadrant=%s%s" % (
        args.out, len(drows), len(data["trail"]), len(sp), quad["quadrant"],
        (" | unavailable: " + ", ".join(errors)) if errors else ""))


if __name__ == "__main__":
    main()
