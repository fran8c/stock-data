"""
Pillar scores as neutral statistics (decided 21 Nov 2026: GitHub provides statistics and news
only; no entries, stops, targets, trade labels or buy/sell wording; the AI harness does the trade
reasoning).

For every stock and every day of the indicator ledger, this records the readings the
Individual Stock Harness v3.2.1 uses for its pillars, as plain counts of bullish and bearish
readings:

  Microstructure (4 readings): Close vs VWAP10; CVD10 sign; UDVR10 label; a divergence, or VT5/20
      above 1.1 with the 5-bar change. Label BULLISH if 3+ readings are bullish, BEARISH if 3+ are
      bearish, otherwise MIXED. (Same thresholds as the harness's pillar test.)
  Momentum (3 readings): Ichimoku score sign; MACD vs signal (or the momentum proxy); RS20 vs the
      main benchmark (leading / lagging). Label BULLISH if 2+ are bullish and neither of the first
      two is bearish; BEARISH likewise; otherwise MIXED.
  Structure (facts, no label): Darvas status (OLD BOX flagged), and the distance from the Close to
      the nearest computed support and resistance, in % and in ATRs. The harness's structure
      pillar also needs an entry and a target, which GitHub does not set.
  Ratings (facts): broker rating changes in the news store in the 14 days to that day, counted as
      upgrades (incl. buy initiations) and downgrades (incl. sell initiations).

Writes docs/data/indicators/<SYMBOL>_scores.csv (one row per day), a "scores" block in
<SYMBOL>_latest.json and a SCORES section in <SYMBOL>_ledger.txt.
"""

import re
from datetime import date, timedelta

import pandas as pd

import indicators as I

WINDOW_DAYS = 14   # ratings window, as the news pack
LABEL = {1: "bullish", -1: "bearish", 0: "neutral"}


# ----------------------------------------------------------------------------
# Broker rating changes (facts from the news store)
# ----------------------------------------------------------------------------
def rating_direction(title):
    t = title.lower()
    if re.search(r"\bdowngrad\w*|\bcut\w* to (sell|underperform|underweight|reduce|hold|neutral|market perform|equal[- ]weight)\b", t):
        return -1
    if re.search(r"\bupgrad\w*|\b(rais|lift)\w* to (buy|outperform|overweight|accumulate|add)\b", t):
        return 1
    if re.search(r"\binitiat\w*", t):
        if re.search(r"\b(buy|outperform|overweight|accumulate|add|strong buy)\b", t):
            return 1
        if re.search(r"\b(sell|underperform|underweight|reduce)\b", t):
            return -1
    return 0


def rating_items(store, item):
    """The stock's broker rating changes in the news store (same story reported twice counts once)."""
    import newsfilter as N
    terms = N.company_terms(item)
    out = []
    for n in sorted(store.values(), key=lambda n: (n["date"], n.get("tag") != "PRIMARY")):
        if n["ticker"] != item["symbol"]:
            continue
        ok, _ = N.about_company(n["title"], terms)
        if not ok:
            continue
        kind, _, _ = N.classify(n["title"], n.get("summary") or "")
        if kind != "RATING CHANGE":
            continue
        direction = rating_direction(n["title"])
        if any(abs((date.fromisoformat(s["date"]) - date.fromisoformat(n["date"])).days) <= 1
               and (s["direction"] == direction or N.similar(s["title"], n["title"]) >= 0.5) for s in out):
            continue
        out.append({"date": n["date"], "title": n["title"], "source": n.get("source"), "url": n.get("url"),
                    "direction": direction,
                    "change": {1: "upgrade", -1: "downgrade", 0: "rating change, no direction"}[direction]})
    return out


# ----------------------------------------------------------------------------
# Readings
# ----------------------------------------------------------------------------
def nz(v):
    return v is None or (isinstance(v, float) and pd.isna(v))


def micro(r):
    a = 0 if nz(r["vwap10"]) else (I.sign(r["close"] - r["vwap10"]) or 0)
    b = 0 if nz(r["cvd10"]) else (I.sign(r["cvd10"]) or 0)
    c = I.udvr_side(r["udvr10_label"])
    if r["divergence"] in ("Bullish", "Bearish"):
        d = 1 if r["divergence"] == "Bullish" else -1
    elif not nz(r["vt5_20"]) and r["vt5_20"] > 1.1 and not nz(r["chg5_pct"]):
        d = I.sign(r["chg5_pct"]) or 0
    else:
        d = 0
    rd = [a, b, c, d]
    if nz(r["cvd10"]) or r["udvr10_label"] is None:
        lab = "INSUFFICIENT DATA"
    else:
        lab = "BULLISH" if rd.count(1) >= 3 else ("BEARISH" if rd.count(-1) >= 3 else "MIXED")
    txt = f"close vs VWAP10 {LABEL[a]}, CVD10 {LABEL[b]}, UDVR10 {LABEL[c]}, divergence/volume trend {LABEL[d]}"
    return rd, lab, txt


def momentum(r):
    a = 0 if nz(r["ichimoku_score"]) else (I.sign(r["ichimoku_score"]) or 0)
    if not nz(r["macd_hist"]):
        b = I.sign(r["macd_hist"]) or 0
    else:
        b = {"bullish": 1, "bearish": -1}.get(r["momentum_proxy"], 0)
    c = {"leading": 1, "lagging": -1}.get(r.get("rs20_main_label"), 0)
    rd = [a, b, c]
    if rd.count(1) >= 2 and a != -1 and b != -1:
        lab = "BULLISH"
    elif rd.count(-1) >= 2 and a != 1 and b != 1:
        lab = "BEARISH"
    else:
        lab = "MIXED"
    rs = r.get("rs20_main")
    txt = (f"Ichimoku {LABEL[a]}, MACD {LABEL[b]}, RS20 {LABEL[c]}"
           f" ({'n/a' if nz(rs) else f'{rs:+.2f} pts'} vs main benchmark)")
    return rd, lab, txt


def structure(r):
    st = r["darvas_status"]
    old = (not nz(r["darvas_old_box"])) and bool(r["darvas_old_box"])
    c, a = r["close"], r["atr14"]
    out = {"darvas_status": None if nz(st) else st, "darvas_old_box": old}
    for side, key in (("support", "nearest_support"), ("resistance", "nearest_resistance")):
        lv = r.get(key)
        if nz(lv):
            out[f"{side}_dist_pct"] = out[f"{side}_dist_atr"] = None
        else:
            out[f"{side}_dist_pct"] = (lv / c - 1) * 100
            out[f"{side}_dist_atr"] = abs(lv - c) / a if not nz(a) and a else None
    return out


def ratings_in_window(ratings, day):
    start = (date.fromisoformat(day) - timedelta(days=WINDOW_DAYS)).isoformat()
    w = [g for g in ratings if start <= g["date"] <= day]
    return w, sum(1 for g in w if g["direction"] > 0), sum(1 for g in w if g["direction"] < 0)


# ----------------------------------------------------------------------------
# Build for one stock
# ----------------------------------------------------------------------------
def build(symbol, daily, latest, market, ratings, cal=None):
    rows, last = [], None
    for i in range(len(daily)):
        r = daily.iloc[i].to_dict()
        if nz(r.get("atr14")):
            continue
        m_rd, m_lab, m_txt = micro(r)
        o_rd, o_lab, o_txt = momentum(r)
        s = structure(r)
        w, up, dn = ratings_in_window(ratings, r["date"])
        row = {"date": r["date"], "close": r["close"], "confluence_final": r["confluence_final"],
               "micro_bullish": m_rd.count(1), "micro_bearish": m_rd.count(-1), "micro_label": m_lab,
               "momentum_bullish": o_rd.count(1), "momentum_bearish": o_rd.count(-1), "momentum_label": o_lab,
               **s, "ratings_upgrades_14d": up, "ratings_downgrades_14d": dn}
        rows.append(row)
        last = (row, m_txt, o_txt, w)
    hist = pd.DataFrame(rows)
    out = None
    if last:
        row, m_txt, o_txt, w = last
        out = {"note": "Statistics only: the readings behind the harness's pillars. Not advice; GitHub sets no entries, "
                       "stops, targets or trade labels. The AI harness does the trade reasoning.",
               "microstructure": {"label": row["micro_label"], "bullish": row["micro_bullish"],
                                  "bearish": row["micro_bearish"], "readings": m_txt},
               "momentum": {"label": row["momentum_label"], "bullish": row["momentum_bullish"],
                            "bearish": row["momentum_bearish"], "readings": o_txt},
               "structure": {k: (I.r4(v) if isinstance(v, float) else v) for k, v in row.items()
                             if k.startswith("darvas") or k.endswith("_dist_pct") or k.endswith("_dist_atr")},
               "ratings_14d": {"upgrades": row["ratings_upgrades_14d"], "downgrades": row["ratings_downgrades_14d"],
                               "items": w}}
    return hist, out


def ledger_lines(sc):
    if not sc:
        return ["--- SCORES ---", "not computed"]
    f = lambda v, s="": "n/a" if v is None else f"{v:+.2f}{s}" if s == "%" else f"{v:.2f}{s}"
    st = sc["structure"]
    L = ["--- SCORES (statistics only, not advice: the readings behind the harness's pillars) ---",
         f"Microstructure: {sc['microstructure']['label']} ({sc['microstructure']['bullish']} bullish / "
         f"{sc['microstructure']['bearish']} bearish of 4) - {sc['microstructure']['readings']}",
         f"Momentum: {sc['momentum']['label']} ({sc['momentum']['bullish']} bullish / {sc['momentum']['bearish']} bearish of 3)"
         f" - {sc['momentum']['readings']}",
         f"Structure: Darvas {st.get('darvas_status') or 'no box'}{' (OLD BOX: context only)' if st.get('darvas_old_box') else ''}; "
         f"nearest support {f(st.get('support_dist_pct'), '%')} ({f(st.get('support_dist_atr'))} ATRs); "
         f"nearest resistance {f(st.get('resistance_dist_pct'), '%')} ({f(st.get('resistance_dist_atr'))} ATRs)",
         f"Broker rating changes, last {WINDOW_DAYS} days: {sc['ratings_14d']['upgrades']} upgrade(s), "
         f"{sc['ratings_14d']['downgrades']} downgrade(s)" +
         ("".join(f"\n  {g['date']} {g['change']}: {g['title'][:90]}" for g in sc["ratings_14d"]["items"]))]
    return L
