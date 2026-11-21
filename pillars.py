"""
Official pillar scores and trade levels (Individual Stock Harness v3.2.1 trade rules).

For every stock and every day of the indicator ledger, this works out:
  - the bias and gates (trade rules 1-2);
  - every allowed setup (rule 3, anchors per rule 5): trend or counter-trend buy, short advisory;
  - levels from the entry and ATR14 with tick rounding (rule 6) and reward-to-risk (rule 7);
  - the fill check against the week-1 and week-2 outlook ranges (rule 7a);
  - the time stop for a fill on the start day (rule 8);
  - the four pillars with each reading (rule 9). Catalyst (pillar 4) uses broker RATING CHANGES
    only (decided 21 Nov 2026): other news is left for the AI, which completes the pillar and
    flags any difference. So a setup's status is one of:
      "proposed on technicals"  - all 3 technical pillars support (meets the minimum whatever the catalyst)
      "depends on catalyst"     - 2 of 3 technical pillars support: proposed only if the catalyst SUPPORTS
      "not proposed"            - fewer than 2 technical pillars support, or the R gate fails
  - the selected buy and short (the setup with the most support, then R gate, then nearest to the Close).

Writes docs/data/indicators/<SYMBOL>_setups.csv (one row per day) and adds a "setups" block to
<SYMBOL>_latest.json and a SETUPS section to <SYMBOL>_ledger.txt.
"""

import math
import re
from datetime import date, timedelta

import pandas as pd

import indicators as I

WINDOW_DAYS = 14   # catalyst window, as the news pack


# ----------------------------------------------------------------------------
# Rounding and levels (rules 6-7)
# ----------------------------------------------------------------------------
def round_tick(x, tick, mode):
    q = x / tick
    q = math.floor(q + 1e-9) if mode == "down" else (math.ceil(q - 1e-9) if mode == "up" else math.floor(q + 0.5))
    return round(q * tick, 6)


def levels(E, A, side, tick):
    e = round_tick(E, tick, "nearest")
    a15, a2, a35 = 1.5 * A, 2.0 * A, 3.5 * A
    if side == "buy":
        stop, tp1, tp2 = e - a15, e + a2, e + a35
        mode = "down"
    else:
        stop, tp1, tp2 = e + a15, e - a2, e - a35
        mode = "up"
    st, t1, t2 = (round_tick(x, tick, mode) for x in (stop, tp1, tp2))
    risk = abs(e - st)
    r1 = abs(t1 - e) / risk if risk else None
    r2 = abs(t2 - e) / risk if risk else None
    return {"entry": e, "stop": st, "tp1": t1, "tp2": t2, "r1": r1, "r2": r2,
            "blended": (r1 + r2) / 2 if r1 and r2 else None, "gate": "PASS" if r2 and r2 >= 2.0 - 1e-9 else "FAIL",
            "arith": f"1.5A {a15:.4f} 2A {a2:.4f} 3.5A {a35:.4f}; stop {e}{'-' if side == 'buy' else '+'}{a15:.4f}={stop:.4f}; "
                     f"TP1 {tp1:.4f}; TP2 {tp2:.4f}"}


# ----------------------------------------------------------------------------
# Catalyst: broker rating changes only
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
    return 0


def rating_items(store, item):
    """The stock's broker rating changes in the news store, with a direction."""
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
            continue   # the same rating change reported again
        out.append({"date": n["date"], "title": n["title"], "source": n.get("source"), "url": n.get("url"),
                    "direction": direction})
    return sorted(out, key=lambda x: x["date"])


# ----------------------------------------------------------------------------
# Pillars (rule 9)
# ----------------------------------------------------------------------------
def all_levels(r):
    names = [("VWAP10", "vwap10"), ("Tenkan", "tenkan"), ("Kijun", "kijun"), ("Kumo top", "kumo_top"),
             ("Kumo bottom", "kumo_bottom"), ("Darvas ceiling", "darvas_ceiling"), ("Darvas floor", "darvas_floor"),
             ("20-bar high", "hh20"), ("20-bar low", "ll20"), ("60-bar high", "hh60"), ("60-bar low", "ll60"),
             ("120-bar high", "hh120"), ("120-bar low", "ll120"), ("52-week high", "high52w"), ("52-week low", "low52w")]
    return [(n, float(r[k])) for n, k in names if r.get(k) is not None and not pd.isna(r[k])]


def mark(v):
    return {1: "favour", -1: "oppose", 0: "neither"}[v]


def pillar_micro(r, x):
    a = (I.sign(r["close"] - r["vwap10"]) or 0) * x if not pd.isna(r["vwap10"]) else 0
    b = (I.sign(r["cvd10"]) or 0) * x if not pd.isna(r["cvd10"]) else 0
    c = I.udvr_side(r["udvr10_label"]) * x
    d = 0
    div = r["divergence"]
    if div in ("Bullish", "Bearish"):
        d = (1 if div == "Bullish" else -1) * x
    elif not pd.isna(r["vt5_20"]) and r["vt5_20"] > 1.1 and not pd.isna(r["chg5_pct"]):
        d = (I.sign(r["chg5_pct"]) or 0) * x
    rd = [a, b, c, d]
    fav, opp = rd.count(1), rd.count(-1)
    res = "SUPPORTS" if fav >= 3 else ("AGAINST" if opp >= 3 else "MIXED")
    if pd.isna(r["cvd10"]) or r["udvr10_label"] is None:
        res = "INSUFFICIENT DATA"
    return res, f"VWAP {mark(a)}, CVD {mark(b)}, UDVR {mark(c)}, div/VT {mark(d)}"


def pillar_momentum(r, x):
    a = (I.sign(r["ichimoku_score"]) or 0) * x if r["ichimoku_score"] is not None and not pd.isna(r["ichimoku_score"]) else 0
    if not pd.isna(r["macd_hist"]):
        b = (I.sign(r["macd_hist"]) or 0) * x
    else:
        b = {"bullish": 1, "bearish": -1}.get(r["momentum_proxy"], 0) * x
    lab = r.get("rs20_main_label")
    c = ({"leading": 1, "lagging": -1}.get(lab, 0)) * x
    rd = [a, b, c]
    fav, opp = rd.count(1), rd.count(-1)
    if fav >= 2 and a != -1 and b != -1:
        res = "SUPPORTS"
    elif opp >= 2:
        res = "AGAINST"
    else:
        res = "MIXED"
    rs = r.get("rs20_main")
    return res, f"Ichimoku {mark(a)}, MACD {mark(b)}, RS20 {mark(c)} ({'n/a' if rs is None or pd.isna(rs) else f'{rs:.2f}'} vs main)"


def pillar_structure(r, x, setup):
    st = r["darvas_status"]
    old = bool(r["darvas_old_box"]) if r["darvas_old_box"] is not None and not pd.isna(r["darvas_old_box"]) else False
    if st is None or (isinstance(st, float) and pd.isna(st)) or st == "Inside" or old:
        a = 0
        dtxt = f"Darvas neither ({st}{' OLD' if old else ''})"
    else:
        a = (1 if st == "Breakout Above" else -1) * x
        dtxt = f"Darvas {mark(a)} ({st})"
    c, E, T1 = r["close"], setup["entry"], setup["tp1"]
    lo, hi = sorted((E, T1))
    if x == 1:   # resistance (above the Close) between entry and TP1
        block = [(n, p) for n, p in all_levels(r) if p > c and lo < p < hi]
    else:        # support (below the Close) between TP1 and entry
        block = [(n, p) for n, p in all_levels(r) if p < c and lo < p < hi]
    b = -1 if block else 1
    e_fav = 1 if setup["anchor_kind"] in ("support", "resistance", "darvas") else 0
    fav = [a, b, e_fav].count(1)
    if fav >= 2 and b == 1:
        res = "SUPPORTS"
    elif b == -1 and (a == -1 or e_fav == -1):
        res = "AGAINST"
    else:
        res = "MIXED"
    btxt = ", ".join(f"{n} {p:.4f}" for n, p in block) or "none"
    return res, f"{dtxt}, room {mark(b)} (blocking: {btxt}), entry {mark(e_fav)}"


def pillar_catalyst(ratings, day, x):
    start = (date.fromisoformat(day) - timedelta(days=WINDOW_DAYS)).isoformat()
    inwin = [g for g in ratings if start <= g["date"] <= day]
    fav = sum(1 for g in inwin if g["direction"] * x > 0)
    opp = sum(1 for g in inwin if g["direction"] * x < 0)
    if not inwin:
        return "FOR THE AI", "no rating changes in the window; other news is for the AI to assess", 0, 0
    prov = "SUPPORTS" if fav > opp else ("AGAINST" if opp > fav else "MIXED")
    return f"PROVISIONAL {prov}", (f"ratings only: {fav} favour, {opp} oppose ("
                                   + "; ".join(f"{g['date']} {g['title'][:70]}" for g in inwin) + "); the AI adds other news"), fav, opp


# ----------------------------------------------------------------------------
# Setups for one day
# ----------------------------------------------------------------------------
def bias_of(final):
    if final.endswith("BULLISH") or "BULLISH (" in final:
        return "BULLISH"
    if final.endswith("BEARISH") or "BEARISH (" in final:
        return "BEARISH"
    if final.startswith("CONFLICTING"):
        return "NEUTRAL / MEAN-REVERTING"
    return "NO TRADE"


def candidate_setups(r, bias):
    c = r["close"]
    lv = {n: p for n, p in all_levels(r)}
    old = bool(r["darvas_old_box"]) if r["darvas_old_box"] is not None and not pd.isna(r["darvas_old_box"]) else False
    out = []

    def add(side, label, anchor, price, kind, order):
        if price is None or pd.isna(price):
            return
        out.append({"side": side, "label": label, "anchor": anchor, "raw_entry": float(price),
                    "anchor_kind": kind, "order": order})

    if bias == "BULLISH":
        add("buy", "TREND BUY", "Close (market)", c, "close", "Market")
        for n in ("VWAP10", "Kijun"):
            if n in lv and lv[n] < c:
                add("buy", "TREND BUY", n, lv[n], "support", "Limit")
        if "Darvas ceiling" in lv and not old and lv["Darvas ceiling"] > c:
            add("buy", "TREND BUY", "Darvas ceiling (breakout)", lv["Darvas ceiling"], "darvas", "Stop")
    if bias in ("BULLISH", "BEARISH", "NEUTRAL / MEAN-REVERTING"):
        lab = "TREND BUY" if bias == "BULLISH" else "COUNTER-TREND BUY"
        sup = {}
        for n in ("20-bar low", "60-bar low", "120-bar low", "52-week low") + (() if old else ("Darvas floor",)):
            if n in lv and lv[n] < c:
                sup.setdefault(round(lv[n], 6), []).append(n)
        for p, names in sorted(sup.items(), reverse=True):
            add("buy", lab, "Limit buy at " + " / ".join(names), p, "support", "Limit")
    if bias == "BEARISH":
        add("short", "SHORT ADVISORY", "Close (market)", c, "close", "Market")
        for n in ("VWAP10", "Kijun"):
            if n in lv and lv[n] > c:
                add("short", "SHORT ADVISORY", n, lv[n], "resistance", "Limit")
        if "Darvas floor" in lv and not old and lv["Darvas floor"] < c:
            add("short", "SHORT ADVISORY", "Darvas floor (breakdown)", lv["Darvas floor"], "darvas", "Stop")
        res = {}
        for n in ("20-bar high", "60-bar high", "120-bar high") + (() if old else ("Darvas ceiling",)):
            if n in lv and lv[n] > c:
                res.setdefault(round(lv[n], 6), []).append(n)
        for p, names in sorted(res.items()):
            add("short", "SHORT ADVISORY", "Limit short at " + " / ".join(names), p, "resistance", "Limit")
    return out


def fill_check(s, r):
    w1l, w1h, w2l, w2h = (r.get(k) for k in ("outlook_week1_low", "outlook_week1_high", "outlook_week2_low", "outlook_week2_high"))
    if w1l is None or pd.isna(w1l) or s["order"] == "Market":
        return ""
    E = s["entry"]
    need_up = (s["side"] == "short" and s["order"] == "Limit") or (s["side"] == "buy" and s["order"] == "Stop")
    out1 = E > w1h if need_up else E < w1l
    if not out1:
        return ""
    out2 = E > w2h if need_up else E < w2l
    return (f"unlikely to fill this week (outside the week-1 range {w1l:.2f} - {w1h:.2f}); "
            + ("also outside the week-2 range" if out2 else "inside the week-2 range")
            + f"; {abs(E / r['close'] - 1) * 100:.1f}% from the Close")


def time_stop(start_day, cal):
    d, n = date.fromisoformat(start_day), 0
    while n < 10:
        d = cal.next_session(d)
        n += 1
    return d.isoformat()


def evaluate_day(r, market, cal, ratings, stale=False):
    tick = I.tick_size(r["close"], market)
    final = r["confluence_final"]
    bias = bias_of(final)
    gates = []
    if pd.isna(r["atr14"]):
        gates.append("ATR14 not computed")
    if (r["signals_available"] or 0) < 3:
        gates.append("fewer than 3 confluence signals")
    if stale:
        gates.append("STALE DATA")
    if bias == "NO TRADE" and not gates:
        gates.append("confluence INSUFFICIENT DATA")
    if gates:
        return {"bias": "NO TRADE", "action": "NO TRADE - " + "; ".join(gates), "setups": [], "buy": None, "short": None}
    A = r["atr14"]
    setups = []
    for s in candidate_setups(r, bias):
        x = 1 if s["side"] == "buy" else -1
        s.update(levels(s["raw_entry"], A, s["side"], tick))
        p1, t1 = pillar_micro(r, x)
        p2, t2 = pillar_momentum(r, x)
        p3, t3 = pillar_structure(r, x, s)
        p4, t4, _, _ = pillar_catalyst(ratings, r["date"], x)
        tech = [p1, p2, p3].count("SUPPORTS")
        if s["gate"] == "FAIL":
            status = "not proposed - RISK/REWARD FAIL"
        elif tech == 3:
            status = "proposed on technicals"
        elif tech == 2:
            status = "depends on catalyst" + (" (provisionally supported by ratings)" if p4 == "PROVISIONAL SUPPORTS" else "")
        else:
            status = "not proposed"
        s.update(p1=p1, p1_readings=t1, p2=p2, p2_readings=t2, p3=p3, p3_readings=t3, p4=p4, p4_readings=t4,
                 technical_support=tech, status=status, fill_check=fill_check(s, r))
        setups.append(s)

    def pick(side):
        cands = [s for s in setups if s["side"] == side]
        if not cands:
            return None
        def key(s):
            live = 3 if s["status"] == "proposed on technicals" else (2 if s["status"].startswith("depends") else 0)
            # among live setups prefer more support; otherwise show the nearest anchor to the Close
            return (-live, -s["technical_support"] if live else 0, s["gate"] != "PASS", abs(s["entry"] - r["close"]))
        return sorted(cands, key=key)[0]
    buy, short = pick("buy"), pick("short")
    buy_ok = buy is not None and not buy["status"].startswith("not proposed")
    short_ok = short is not None and not short["status"].startswith("not proposed")
    if buy_ok:
        action = ("COUNTER-TREND BUY SETUP" if buy["label"].startswith("COUNTER") else "BUY SETUP")
        if buy["status"].startswith("depends"):
            action += " (if the catalyst pillar supports)"
    elif bias == "BEARISH":
        action = "AVOID" + (" - short advisory below" + (" (if the catalyst pillar supports)" if short["status"].startswith("depends") else "")
                            if short_ok else "")
    else:
        action = "WAIT"
    return {"bias": bias, "action": action, "setups": setups, "buy": buy, "short": short}


# ----------------------------------------------------------------------------
# Build for one stock: every day + latest detail
# ----------------------------------------------------------------------------
def build(symbol, daily, latest, market, ratings, cal=None):
    cal = cal or I.read_calendar(market)
    rows = []
    last_ev = None
    for i in range(len(daily)):
        r = daily.iloc[i].to_dict()
        if pd.isna(r.get("atr14")):
            continue
        stale = False
        if i == len(daily) - 1 and latest.get("outlook"):
            ol = latest["outlook"]
            stale = len(ol.get("missing_sessions") or []) >= 2
            # the latest day's outlook counts any missing session (e.g. a late bar)
            for k in ("week1_low", "week1_high", "week2_low", "week2_high"):
                r[f"outlook_{k}"] = ol[k]
        ev = evaluate_day(r, market, cal, ratings, stale)
        row = {"date": r["date"], "close": r["close"], "confluence_final": r["confluence_final"], "bias": ev["bias"],
               "action": ev["action"]}
        for side in ("buy", "short"):
            s = ev[side]
            for k in ("label", "anchor", "order", "entry", "stop", "tp1", "tp2", "r2", "gate", "p1", "p2", "p3", "p4",
                      "technical_support", "status", "fill_check"):
                row[f"{side}_{k}"] = s.get(k) if s else None
        rows.append(row)
        last_ev = ev
    hist = pd.DataFrame(rows)
    # latest-day detail
    out = None
    if last_ev is not None:
        ol = latest.get("outlook") or {}
        ts = time_stop(ol["start_day"], cal) if ol.get("start_day") else None
        out = {"bias": last_ev["bias"], "action": last_ev["action"], "start_day": ol.get("start_day"),
               "time_stop_if_filled_on_start_day": ts,
               "catalyst_rule": "rating changes only; the AI completes the catalyst pillar with other news and flags any difference",
               "rating_changes_in_window": [g for g in ratings
                                            if (date.fromisoformat(latest["last_bar"]) - timedelta(days=WINDOW_DAYS)).isoformat()
                                            <= g["date"] <= latest["as_of"]],
               "selected_buy": _clean(last_ev["buy"]), "selected_short": _clean(last_ev["short"]),
               "tested": [_clean(s) for s in last_ev["setups"]]}
    return hist, out


def _clean(s):
    if not s:
        return None
    return {k: (I.r4(v) if isinstance(v, float) else v) for k, v in s.items() if k != "raw_entry"}


def ledger_lines(setups):
    if not setups:
        return ["--- SETUPS ---", "not computed"]
    L = ["--- SETUPS (official; trade rules v3.2.1; catalyst = rating changes only, the AI completes it) ---",
         f"Bias {setups['bias']} | Action {setups['action']} | start day {setups['start_day']} | "
         f"time stop for a fill on the start day: close of {setups['time_stop_if_filled_on_start_day']}"]
    rc = setups["rating_changes_in_window"]
    L.append("Rating changes in the window: " + ("; ".join(f"{g['date']} {g['title'][:80]} ({ {1: 'up', -1: 'down', 0: 'no direction'}[g['direction']]})"
                                                          for g in rc) if rc else "none"))
    for s in setups["tested"]:
        L.append(f"SETUP {s['label']} - {s['anchor']} ({s['order']}) E {s['entry']}: stop {s['stop']} TP1 {s['tp1']} TP2 {s['tp2']} "
                 f"R1 {s['r1']:.3f} R2 {s['r2']:.3f} gate {s['gate']} -> {s['status']}" + (f" | {s['fill_check']}" if s["fill_check"] else ""))
        L.append(f"  P1 Micro {s['p1']}: {s['p1_readings']}")
        L.append(f"  P2 Momentum {s['p2']}: {s['p2_readings']}")
        L.append(f"  P3 Structure {s['p3']}: {s['p3_readings']}")
        L.append(f"  P4 Catalyst {s['p4']}: {s['p4_readings']}")
        L.append(f"  Arithmetic: {s['arith']}")
    for side in ("buy", "short"):
        sel = setups[f"selected_{side}"]
        L.append(f"Selected {side}: " + (f"{sel['anchor']} E {sel['entry']} | {sel['status']}" if sel else "none"))
    return L
