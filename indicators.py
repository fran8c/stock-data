"""
Daily indicator ledger for the Individual Stock Harness (v3.2 definitions).

For each watchlist stock, pipeline.py calls build() with its daily bars, its
dividends and its comparators (benchmarks and peers). build() works out every
indicator for EVERY day, so each figure has a full daily history, and writes:

  docs/data/indicators/<SYMBOL>.csv          one row per trading day (about a year)
  docs/data/indicators/<SYMBOL>_weeks.csv    one row per Monday-to-Friday week
  docs/data/indicators/<SYMBOL>_latest.json  the latest day in full (levels, outlook, ...)
  docs/data/indicators/<SYMBOL>_ledger.txt   the latest day as a readable ledger
  docs/data/indicators/COLUMNS.md            what every column means

These are the official figures. The harness checks against them and flags any
difference. Formulas follow harnesses/Individual Stock Harness v3.2 <definitions>:
  CLV, VolDelta, CVD10, VWAP10, volume ratio, UDVR10, VT5/20, volume confirmation,
  ATR14 (Wilder), MACD 12/26/9, momentum proxy, RS20, 52-week and 20/60/120-bar
  highs and lows, Ichimoku, Darvas, confluence, divergence, weekly recap,
  outlook ranges (holiday-aware), reference levels, range calibration and
  ex-dividend adjustment.

Run on its own for a check:  python indicators.py D05.SI   (uses docs/data files)
"""

import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "docs" / "data"
OUTDIR = DATA / "indicators"
HISTORY_DAYS = 365          # rows kept in <SYMBOL>.csv (computed from up to 5 years of bars)
SQRT5 = 2.236
SQRT10 = 3.162


# ----------------------------------------------------------------------------
# Calendar
# ----------------------------------------------------------------------------
class Calendar:
    """Weekdays, minus closures; early closes count as half a day."""

    def __init__(self, closed=(), early=()):
        self.closed = set(closed)
        self.early = set(early)

    def weight(self, d):
        if d.weekday() >= 5 or d.isoformat() in self.closed:
            return 0.0
        return 0.5 if d.isoformat() in self.early else 1.0

    def is_session(self, d):
        return self.weight(d) > 0

    def next_session(self, d):
        d += timedelta(days=1)
        while not self.is_session(d):
            d += timedelta(days=1)
        return d

    def sessions(self, after, through):
        """Session days with after < day <= through."""
        out, d = [], after + timedelta(days=1)
        while d <= through:
            if self.is_session(d):
                out.append(d)
            d += timedelta(days=1)
        return out


def read_calendar(market, path=ROOT / "holidays.txt"):
    """holidays.txt: MARKET | YYYY-MM-DD | name [| EARLY CLOSE]."""
    closed, early = [], []
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            p = [x.strip() for x in s.split("|")]
            if len(p) >= 2 and p[0].upper() == market:
                (early if len(p) >= 4 and "EARLY" in p[3].upper() else closed).append(p[1])
    return Calendar(closed, early)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def tick_size(price, market):
    if market == "SGX":
        return 0.001 if price < 0.20 else (0.005 if price < 1.00 else 0.01)
    return 0.0001 if price < 1.00 else 0.01


def r4(x):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    return None if math.isnan(f) or math.isinf(f) else round(f, 4)


INT_COLS = {"volume", "up_days10", "down_days10", "ichimoku_score", "darvas_age_bars", "darvas_crossing_bars_ago",
            "signal_vwap", "signal_ichimoku", "signal_macd", "signal_cvd", "bull_signals", "bear_signals",
            "signals_available", "bars_in_52w_window"}


def as_int(key, v):
    """Whole-number fields as integers (pandas stores them as floats next to blanks)."""
    if key in INT_COLS and isinstance(v, float) and not math.isnan(v) and v == int(v):
        return int(v)
    return v


def udvr_label(v, up_vol, down_vol):
    if down_vol == 0 and up_vol > 0:
        return "all up days (ratio not defined)"
    if up_vol == 0 and down_vol > 0:
        return "all down days (ratio not defined)"
    if v is None:
        return None
    return "buying-dominated" if v > 1.1 else ("selling-dominated" if v < 0.9 else "balanced")


def udvr_side(label):
    if not label:
        return 0
    if label.startswith("buying") or label.startswith("all up"):
        return 1
    if label.startswith("selling") or label.startswith("all down"):
        return -1
    return 0


def vol_band(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return "quieter than usual" if x < 0.9 else ("busier" if x > 1.1 else "normal")


def rs_label(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return "leading" if x > 1.0 else ("lagging" if x < -1.0 else "in line")


def regime(score):
    if score is None:
        return None
    if score >= 3:
        return "Strong Bullish"
    if score >= 1:
        return "Weak Bullish"
    if score == 0:
        return "Neutral Consolidation"
    if score >= -2:
        return "Weak Bearish"
    return "Strong Bearish"


def sign(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return 1 if x > 0 else (-1 if x < 0 else 0)


def verdict(signals):
    """signals: list of +1/-1/0/None. Returns (label, bull, bear, available)."""
    avail = [s for s in signals if s is not None]
    bull, bear = sum(1 for s in avail if s > 0), sum(1 for s in avail if s < 0)
    if len(avail) < 3:
        return "INSUFFICIENT DATA", bull, bear, len(avail)
    if bull >= 3 and bear == 0:
        return "HIGH BULLISH", bull, bear, len(avail)
    if bear >= 3 and bull == 0:
        return "HIGH BEARISH", bull, bear, len(avail)
    if bull > bear:
        return "LEAN BULLISH", bull, bear, len(avail)
    if bear > bull:
        return "LEAN BEARISH", bull, bear, len(avail)
    return "CONFLICTING", bull, bear, len(avail)


# ----------------------------------------------------------------------------
# Daily series
# ----------------------------------------------------------------------------
def prepare(bars):
    df = bars.copy()
    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    ok = (df["High"] >= df[["Open", "Close"]].max(axis=1)) & (df["Low"] <= df[["Open", "Close"]].min(axis=1)) \
        & (df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)
    bad = df.loc[~ok, "Date"].tolist()
    return df[ok].reset_index(drop=True), bad


def div_series(divs, dates):
    """Dividend amount by ex-date, aligned to the bar dates (ex-dates on non-bar days are dropped)."""
    out = pd.Series(0.0, index=dates)
    if divs is not None and len(divs):
        d = divs[divs["Type"].astype(str).str.lower().str.startswith("div")]
        for dt, amt in zip(pd.to_datetime(d["Date"]).dt.strftime("%Y-%m-%d"), d["Amount"]):
            if dt in out.index:
                out[dt] += float(amt)
    return out


def comp_close_on_or_before(comp, dates):
    """comp: Series of Close indexed by date string. Returns (close, date used) for each date."""
    s = comp.sort_index()
    idx = s.index.searchsorted(pd.Index(dates), side="right") - 1
    vals, used = [], []
    for i in idx:
        if i < 0:
            vals.append(math.nan)
            used.append(None)
        else:
            vals.append(float(s.iloc[i]))
            used.append(s.index[i])
    return vals, used


def compute_daily(df, divs=None, comparators=None, main=None):
    """
    df: Date, Open, High, Low, Close, Volume (validated, ascending).
    comparators: {name: Series of Close by date string}; main: name of the main comparator.
    Returns a DataFrame with one row per bar and every daily indicator.
    """
    comparators = comparators or {}
    o, h, l, c, v = (df[k].astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    n = len(df)
    out = pd.DataFrame({"date": df["Date"], "open": o, "high": h, "low": l, "close": c, "volume": v})
    dv = div_series(divs, df["Date"]).values
    out["dividend"] = dv
    prev = c.shift(1)
    out["chg_raw_pct"] = (c / prev - 1) * 100
    out["chg_pct"] = ((c + dv) / prev - 1) * 100            # ex-dividend adjusted
    out["gap_pct"] = (o / prev - 1) * 100

    rng = h - l
    clv = (((c - l) - (h - c)) / rng).where(rng != 0, 0.0)
    out["clv"] = clv
    out["vol_delta"] = v * clv
    out["cvd10"] = out["vol_delta"].rolling(10).sum()
    tp = (h + l + c) / 3
    out["vwap10"] = (tp * v).rolling(10).sum() / v.rolling(10).sum()
    out["close_vs_vwap10_pct"] = (c / out["vwap10"] - 1) * 100
    avg20 = v.shift(1).rolling(20).mean()
    out["avg_vol20_prior"] = avg20
    out["vol_ratio"] = v / avg20

    # UDVR10 with ex-dividend-adjusted up/down classification
    up = out["chg_pct"] > 0
    dn = out["chg_pct"] < 0
    upv = (v * up).rolling(10).sum()
    dnv = (v * dn).rolling(10).sum()
    out["up_days10"] = up.astype(float).rolling(10).sum()
    out["down_days10"] = dn.astype(float).rolling(10).sum()
    out["up_vol10"] = upv
    out["down_vol10"] = dnv
    out["udvr10"] = (upv / dnv).where(dnv > 0)
    out.loc[out.index < 10, ["udvr10", "up_days10", "down_days10", "up_vol10", "down_vol10"]] = math.nan
    out["udvr10_label"] = [udvr_label(r, u, d) if i >= 10 else None
                           for i, (r, u, d) in enumerate(zip(out["udvr10"], upv, dnv))]

    # Volume trend and windowed changes (adjusted for dividends inside the window)
    mean5 = v.rolling(5).mean()
    mean20b = v.shift(5).rolling(20).mean()
    out["vt5_20"] = mean5 / mean20b
    out["vt5_20_label"] = [vol_band(x) for x in out["vt5_20"]]
    cumdiv = pd.Series(dv).cumsum()

    def win_chg(k, adjust=True):
        start = c.shift(k)
        d = (cumdiv - cumdiv.shift(k)) if adjust else 0
        return ((c + d) / start - 1) * 100

    out["chg5_raw_pct"] = win_chg(5, False)
    out["chg5_pct"] = win_chg(5)
    out["chg10_raw_pct"] = win_chg(10, False)
    out["chg10_pct"] = win_chg(10)
    out["chg20_raw_pct"] = win_chg(20, False)
    out["chg20_pct"] = win_chg(20)

    # Divergence
    div = []
    for p, cv in zip(out["chg10_pct"], out["cvd10"]):
        if pd.isna(p) or pd.isna(cv):
            div.append(None)
        elif p > 0 and cv < 0:
            div.append("Bearish")
        elif p < 0 and cv > 0:
            div.append("Bullish")
        else:
            div.append("None")
    out["divergence"] = div

    # ATR14 (Wilder): seed = mean of the first 14 True Ranges, then (prev*13 + TR)/14
    tr = pd.concat([rng, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = math.nan
    atr = [math.nan] * n
    if n >= 15:
        atr[14] = tr.iloc[1:15].mean()
        for i in range(15, n):
            atr[i] = (atr[i - 1] * 13 + tr.iloc[i]) / 14
    out["true_range"] = tr
    out["atr14"] = atr
    out["atr14_pct_of_close"] = out["atr14"] / c * 100
    out["range5_low"] = c - out["atr14"] * SQRT5
    out["range5_high"] = c + out["atr14"] * SQRT5

    # MACD 12/26/9 (EMA, adjust=False) and the momentum proxy
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()
    out["macd"] = macd
    out["macd_signal"] = sig
    out["macd_hist"] = macd - sig
    out.loc[out.index < 34, ["macd", "macd_signal", "macd_hist"]] = math.nan
    out["macd_note"] = ["indicative, not converged" if i < 99 else "" for i in range(n)]
    sma20 = c.rolling(20).mean()
    out["sma20"] = sma20
    mp = []
    for cl, s20, ch in zip(c, sma20, out["chg10_pct"]):
        if pd.isna(s20) or pd.isna(ch):
            mp.append(None)
        else:
            a, b = sign(cl - s20), sign(ch)
            mp.append("bullish" if a == 1 and b == 1 else ("bearish" if a == -1 and b == -1 else "mixed"))
    out["momentum_proxy"] = mp

    # Highs and lows
    for k in (20, 60, 120):
        out[f"hh{k}"] = h.rolling(k).max()
        out[f"ll{k}"] = l.rolling(k).min()
    dts = pd.to_datetime(df["Date"])
    hi52, lo52, hi52d, lo52d, n52 = [], [], [], [], []
    j = 0
    for i in range(n):
        while dts.iloc[j] < dts.iloc[i] - pd.Timedelta(days=364):
            j += 1
        n52.append(i - j + 1)
        if i - j + 1 >= 240:
            wh, wl = h.iloc[j:i + 1], l.iloc[j:i + 1]
            hi52.append(wh.max()); lo52.append(wl.min())
            hi52d.append(df["Date"].iloc[j + int(wh.values.argmax())])
            lo52d.append(df["Date"].iloc[j + int(wl.values.argmin())])
        else:
            hi52.append(math.nan); lo52.append(math.nan); hi52d.append(None); lo52d.append(None)
    out["high52w"], out["high52w_date"], out["low52w"], out["low52w_date"] = hi52, hi52d, lo52, lo52d
    out["bars_in_52w_window"] = n52
    out["from_high52w_pct"] = (c / out["high52w"] - 1) * 100
    out["from_low52w_pct"] = (c / out["low52w"] - 1) * 100

    # Ichimoku (needs 78 bars)
    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
    out["tenkan"], out["kijun"], out["span_a"], out["span_b"] = tenkan, kijun, span_a, span_b
    kumo, tk, chik, score, squeeze = [], [], [], [], []
    for i in range(n):
        if i < 77 or pd.isna(span_b.iloc[i]) or pd.isna(atr[i]):
            kumo.append(None); tk.append(None); chik.append(None); score.append(None); squeeze.append(None)
            continue
        top, bot = max(span_a.iloc[i], span_b.iloc[i]), min(span_a.iloc[i], span_b.iloc[i])
        k = 1 if c.iloc[i] > top else (-1 if c.iloc[i] < bot else 0)
        sq = abs(tenkan.iloc[i] - kijun.iloc[i]) < 0.25 * atr[i]
        t = 0 if sq else (1 if tenkan.iloc[i] > kijun.iloc[i] else -1)
        ch = 1 if c.iloc[i] > h.iloc[i - 26] else (-1 if c.iloc[i] < l.iloc[i - 26] else 0)
        kumo.append({1: "Above", -1: "Below", 0: "Inside"}[k])
        tk.append("Neutral Squeeze" if sq else ("Bullish" if t == 1 else "Bearish"))
        chik.append({1: "Bullish Unobstructed", -1: "Bearish Unobstructed", 0: "Obstructed"}[ch])
        score.append(k + t + ch)
        squeeze.append(sq)
    out["kumo_top"] = pd.concat([span_a, span_b], axis=1).max(axis=1)
    out["kumo_bottom"] = pd.concat([span_a, span_b], axis=1).min(axis=1)
    out["price_vs_kumo"], out["tk_state"], out["chikou"] = kumo, tk, chik
    out["ichimoku_score"] = score
    out["ichimoku_regime"] = [regime(s) for s in score]

    # Darvas box (needs 64 bars)
    dar = [darvas_at(df, out["vol_ratio"], i) for i in range(n)]
    for key in ("ceiling", "ceiling_date", "floor", "status", "age_bars", "old_box",
                "crossing_date", "crossing_bars_ago", "crossing_vol_ratio", "crossing_volume_label"):
        out[f"darvas_{key}"] = [d.get(key) if d else None for d in dar]

    # 20-day relative strength vs every comparator (stock change adjusted, comparator raw)
    start_dates = df["Date"].shift(20)
    for name, comp in comparators.items():
        end_c, end_d = comp_close_on_or_before(comp, df["Date"])
        st_c, _ = comp_close_on_or_before(comp, [d if isinstance(d, str) else "0000-00-00" for d in start_dates])
        cchg = (pd.Series(end_c) / pd.Series(st_c) - 1) * 100
        cchg[start_dates.isna().values] = math.nan
        key = name.replace("^", "").replace(".", "_")
        out[f"rs20_{key}"] = out["chg20_pct"].values - cchg.values
        out[f"chg20_{key}_pct"] = cchg.values
        out[f"{key}_close_date"] = end_d
    if main:
        mk = main.replace("^", "").replace(".", "_")
        out["rs20_main"] = out.get(f"rs20_{mk}")
        out["rs20_main_label"] = [rs_label(x) for x in out["rs20_main"]] if "rs20_main" in out else None

    # Confluence and volume confirmation
    conf, nb, ns, na, vc, final = [], [], [], [], [], []
    for i in range(n):
        s_v = sign(c.iloc[i] - out["vwap10"].iloc[i]) if not pd.isna(out["vwap10"].iloc[i]) else None
        s_i = sign(score[i]) if score[i] is not None else None
        s_m = sign(out["macd"].iloc[i] - out["macd_signal"].iloc[i]) if not pd.isna(out["macd"].iloc[i]) else (
            {"bullish": 1, "bearish": -1, "mixed": 0}.get(mp[i]) if mp[i] else None)
        s_c = sign(out["cvd10"].iloc[i]) if not pd.isna(out["cvd10"].iloc[i]) else None
        lab, b, s, a = verdict([s_v, s_i, s_m, s_c])
        conf.append(lab); nb.append(b); ns.append(s); na.append(a)
        if lab.startswith("HIGH") or lab.startswith("LEAN"):
            side = 1 if lab.endswith("BULLISH") else -1
            cs, us = sign(out["cvd10"].iloc[i]) or 0, udvr_side(out["udvr10_label"].iloc[i])
            vt = out["vt5_20"].iloc[i]
            if cs == side and us == side and not pd.isna(vt) and vt >= 0.9:
                v_c = "CONFIRMS"
            elif cs == -side and us == -side:
                v_c = "CONTRADICTS"
            else:
                v_c = "NEUTRAL"
        else:
            v_c = "NOT APPLICABLE"
        vc.append(v_c)
        if v_c == "CONTRADICTS" and lab.startswith("HIGH"):
            final.append(lab.replace("HIGH", "LEAN") + " (downgraded: volume contradicts)")
        else:
            final.append(lab)
    out["signal_vwap"] = [sign(x) if not pd.isna(x) else None for x in (c - out["vwap10"])]
    out["signal_ichimoku"] = [sign(s) if s is not None else None for s in score]
    out["signal_macd"] = [sign(x) if not pd.isna(x) else None for x in out["macd_hist"]]
    out["signal_cvd"] = [sign(x) if not pd.isna(x) else None for x in out["cvd10"]]
    out["confluence"], out["bull_signals"], out["bear_signals"], out["signals_available"] = conf, nb, ns, na
    out["volume_confirmation"] = vc
    out["confluence_final"] = final
    return out


def darvas_at(df, vol_ratio, t):
    """Latest Darvas box as of bar t (harness definition), or None."""
    if t < 63:
        return None
    h, l, c = df["High"].values, df["Low"].values, df["Close"].values
    for i in range(t - 3, -1, -1):
        lo = max(0, i - 59)
        if h[i] >= h[lo:i + 1].max() and h[i + 1:i + 4].max() <= h[i]:
            ceil, floor = h[i], l[i:i + 4].min()
            status = "Breakout Above" if c[t] > ceil else ("Breakdown Below" if c[t] < floor else "Inside")
            age = t - i
            out = {"ceiling": ceil, "ceiling_date": df["Date"].iloc[i], "floor": floor, "status": status,
                   "age_bars": age, "old_box": age > 60}
            if status != "Inside":
                for j in range(i + 4, t + 1):
                    if (status == "Breakout Above" and c[j] > ceil) or (status == "Breakdown Below" and c[j] < floor):
                        vr = vol_ratio.iloc[j]
                        out.update(crossing_date=df["Date"].iloc[j], crossing_bars_ago=t - j,
                                   crossing_vol_ratio=None if pd.isna(vr) else float(vr),
                                   crossing_volume_label=None if pd.isna(vr) else (
                                       "institutional participation" if vr >= 1.5 else (
                                           "fakeout / liquidity-trap risk" if vr < 1.0 else "inconclusive")))
                        break
            return out
    return None


# ----------------------------------------------------------------------------
# Weekly recap
# ----------------------------------------------------------------------------
def weekly(df, daily, comparators, as_of):
    dts = pd.to_datetime(df["Date"])
    monday = (dts - pd.to_timedelta(dts.dt.weekday, unit="D")).dt.strftime("%Y-%m-%d")
    c, v = daily["close"], daily["volume"]
    rows = []
    for wk, idx in daily.groupby(monday).groups.items():
        idx = sorted(idx)
        first, last = idx[0], idx[-1]
        fri = (pd.Timestamp(wk) + pd.Timedelta(days=4)).date()
        prev_c = c.iloc[first - 1] if first > 0 else None
        d_in = daily["dividend"].iloc[first:last + 1].sum()
        base = daily["open"].iloc[first] if prev_c is None else prev_c
        w = {"week_of": wk, "complete": as_of > fri, "bars": len(idx),
             "first_date": df["Date"].iloc[first], "last_date": df["Date"].iloc[last],
             "open": daily["open"].iloc[first], "close": c.iloc[last],
             "high": daily["high"].iloc[first:last + 1].max(), "low": daily["low"].iloc[first:last + 1].min(),
             "prev_close": prev_c,
             "chg_raw_pct": (c.iloc[last] / base - 1) * 100,
             "chg_pct": ((c.iloc[last] + d_in) / base - 1) * 100,
             "dividends_in_week": d_in}
        chg = daily["chg_pct"].iloc[first:last + 1]
        vol = v.iloc[first:last + 1]
        w["up_days"] = int((chg > 0).sum())
        w["down_days"] = int((chg < 0).sum())
        upv, dnv = float(vol[chg > 0].sum()), float(vol[chg < 0].sum())
        w["up_vol"], w["down_vol"] = upv, dnv
        w["up_down_vol_ratio"] = upv / dnv if dnv > 0 else None
        w["up_down_vol_label"] = udvr_label(w["up_down_vol_ratio"], upv, dnv)
        w["avg_volume"] = vol.mean()
        w["base20_volume"] = v.iloc[first - 20:first].mean() if first >= 20 else None
        w["volume_vs_normal"] = w["avg_volume"] / w["base20_volume"] if w["base20_volume"] else None
        w["volume_label"] = vol_band(w["volume_vs_normal"])
        w["week_cvd"] = daily["vol_delta"].iloc[first:last + 1].sum()
        hl = w["high"] - w["low"]
        w["close_position"] = (w["close"] - w["low"]) / hl if hl else None
        sd = first + int(vol.values.argmax())
        w["standout_date"] = df["Date"].iloc[sd]
        w["standout_volume"] = v.iloc[sd]
        w["standout_chg_pct"] = daily["chg_pct"].iloc[sd]
        w["standout_clv"] = daily["clv"].iloc[sd]
        w["standout_vol_ratio"] = daily["vol_ratio"].iloc[sd]
        for name, comp in comparators.items():
            key = name.replace("^", "").replace(".", "_")
            ref_d = df["Date"].iloc[first - 1] if first > 0 else None
            e, ed = comp_close_on_or_before(comp, [df["Date"].iloc[last]])
            if ref_d:
                s, _ = comp_close_on_or_before(comp, [ref_d])
                cc = (e[0] / s[0] - 1) * 100 if s[0] and not math.isnan(s[0]) else None
            else:
                cc = None
            w[f"{key}_chg_pct"] = cc
            w[f"{key}_to"] = ed[0]
            w[f"{key}_rel_pts"] = w["chg_pct"] - cc if cc is not None else None
        rows.append(w)
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Latest-day extras: outlook, reference levels, calibration
# ----------------------------------------------------------------------------
def outlook(close, atr, last_bar, expected, cal):
    lb = date.fromisoformat(last_bar)
    ref = max(lb, date.fromisoformat(expected)) if expected else lb
    start = cal.next_session(ref)
    w1_end = start + timedelta(days=4 - start.weekday())
    w2_end = w1_end + timedelta(days=7)
    d1 = sum(cal.weight(d) for d in cal.sessions(lb, w1_end))
    d2 = d1 + sum(cal.weight(d) for d in cal.sessions(w1_end, w2_end))
    s1, s2 = round(math.sqrt(d1), 3), round(math.sqrt(d2), 3)
    missing = [d.isoformat() for d in cal.sessions(lb, ref)]
    closures = [d.isoformat() for d in pd.date_range(start, w2_end).date
                if d.weekday() < 5 and not cal.is_session(d)]
    early = [d.isoformat() for d in pd.date_range(start, w2_end).date if cal.weight(d) == 0.5]
    return {"start_day": start.isoformat(), "week1_end": w1_end.isoformat(), "week2_end": w2_end.isoformat(),
            "missing_sessions": missing, "closures": closures, "early_closes": early,
            "d1": d1, "d2": d2, "sqrt_d1": s1, "sqrt_d2": s2,
            "week1_low": close - atr * s1, "week1_high": close + atr * s1,
            "week2_low": close - atr * s2, "week2_high": close + atr * s2}


def reference_levels(row, market):
    c = row["close"]
    cand = [("VWAP10", row["vwap10"]), ("Tenkan", row["tenkan"]), ("Kijun", row["kijun"]),
            ("Kumo top", row["kumo_top"]), ("Kumo bottom", row["kumo_bottom"]),
            ("Darvas ceiling", row["darvas_ceiling"]), ("Darvas floor", row["darvas_floor"]),
            ("20-bar high", row["hh20"]), ("20-bar low", row["ll20"]),
            ("60-bar high", row["hh60"]), ("60-bar low", row["ll60"]),
            ("120-bar high", row["hh120"]), ("120-bar low", row["ll120"]),
            ("52-week high", row["high52w"]), ("52-week low", row["low52w"])]
    cand = [(n, float(p)) for n, p in cand if p is not None and not pd.isna(p)]
    tick = tick_size(c, market)
    merged = []
    for name, p in sorted(cand, key=lambda x: x[1]):
        if merged and abs(p - merged[-1]["price"]) <= tick + 1e-9:
            merged[-1]["names"].append(name)
        else:
            merged.append({"names": [name], "price": p})
    for m in merged:
        m["name"] = " / ".join(m["names"])
        m["dist_pct"] = (m["price"] / c - 1) * 100
    above = [m for m in merged if m["price"] > c][:3]
    below = [m for m in merged if m["price"] < c][::-1][:3]
    notes = []
    atr = row["atr14"]
    if not below:
        at = "52-week" if not pd.isna(row["low52w"]) else "120-bar"
        notes.append(f"No computed support below: the price is at its {at} low")
    elif atr and abs(c - below[0]["price"]) > 3 * atr:
        notes.append(f"nearest support is distant: {abs(c - below[0]['price']) / atr:.1f} ATRs")
    if not above:
        at = "52-week" if not pd.isna(row["high52w"]) else "120-bar"
        notes.append(f"No computed resistance above: the price is at its {at} high")
    elif atr and abs(above[0]["price"] - c) > 3 * atr:
        notes.append(f"nearest resistance is distant: {abs(above[0]['price'] - c) / atr:.1f} ATRs")
    clean = lambda lst: [{"name": m["name"], "price": r4(m["price"]), "dist_pct": r4(m["dist_pct"])} for m in lst]
    return clean(above), clean(below), notes


def calibration(daily, since=None):
    c, a = daily["close"].values, daily["atr14"].values
    dates = daily["date"].values
    out = {}
    for k, s in ((5, math.sqrt(5)), (10, math.sqrt(10))):   # exact roots, as the harness says sqrt(5)
        inside = total = 0
        for t in range(len(c) - k):
            if math.isnan(a[t]) or (since and dates[t] < since):
                continue
            total += 1
            inside += abs(c[t + k] - c[t]) <= a[t] * s
        out[f"{k}d_inside"], out[f"{k}d_windows"] = inside, total
        out[f"{k}d_share_pct"] = r4(inside / total * 100) if total else None
    return out


# ----------------------------------------------------------------------------
# Build and write
# ----------------------------------------------------------------------------
def build(symbol, bars, divs, comparators, main, market, as_of, expected, cal=None, history_days=HISTORY_DAYS):
    """
    bars: DataFrame Date, Open, High, Low, Close, Volume (up to 5 years is best).
    comparators: {name: DataFrame with Date, Close}. Returns (daily, weeks, latest).
    """
    cal = cal or read_calendar(market)
    df, bad = prepare(bars)
    comp = {}
    for name, cdf in comparators.items():
        s = cdf.copy()
        s["Date"] = pd.to_datetime(s["Date"]).dt.strftime("%Y-%m-%d")
        comp[name] = s.drop_duplicates("Date", keep="last").set_index("Date")["Close"].astype(float).sort_index()
    daily = compute_daily(df, divs, comp, main)

    # Outlook ranges for every day (each day's own next two weeks)
    w1l, w1h, w2l, w2h, d1s, d2s = [], [], [], [], [], []
    for _, r in daily.iterrows():
        if pd.isna(r["atr14"]):
            for lst in (w1l, w1h, w2l, w2h, d1s, d2s):
                lst.append(None)
            continue
        ol = outlook(r["close"], r["atr14"], r["date"], r["date"], cal)
        w1l.append(ol["week1_low"]); w1h.append(ol["week1_high"]); w2l.append(ol["week2_low"])
        w2h.append(ol["week2_high"]); d1s.append(ol["d1"]); d2s.append(ol["d2"])
    daily["outlook_d1"], daily["outlook_week1_low"], daily["outlook_week1_high"] = d1s, w1l, w1h
    daily["outlook_d2"], daily["outlook_week2_low"], daily["outlook_week2_high"] = d2s, w2l, w2h
    sup, res = [], []
    for _, r in daily.iterrows():
        ab, be, _ = reference_levels(r, market)
        res.append(ab[0]["price"] if ab else None)
        sup.append(be[0]["price"] if be else None)
    daily["nearest_resistance"], daily["nearest_support"] = res, sup

    as_of_d = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    weeks = weekly(df, daily, comp, as_of_d)

    last = daily.iloc[-1]
    above, below, notes = reference_levels(last, market)
    ol = outlook(last["close"], last["atr14"], last["date"], expected, cal) if not pd.isna(last["atr14"]) else None
    year_start = (date.fromisoformat(last["date"]) - timedelta(days=364)).isoformat()
    comps = {}
    for name, s in comp.items():
        key = name.replace("^", "").replace(".", "_")
        comps[name] = {"last_date": s.index[-1] if len(s) else None,
                       "sessions_behind_stock": int((df["Date"] > s.index[-1]).sum()) if len(s) else None,
                       "rs20_pts": r4(last.get(f"rs20_{key}")), "chg20_pct": r4(last.get(f"chg20_{key}_pct")),
                       "label": rs_label(last.get(f"rs20_{key}"))}
    latest = {
        "symbol": symbol, "market": market, "as_of": as_of_d.isoformat(), "expected_session": expected,
        "last_bar": last["date"], "bars_used": len(df), "bars_excluded": bad,
        "currency": "SGD" if market == "SGX" else "USD", "tick": tick_size(last["close"], market),
        "main_comparator": main, "comparators": comps,
        "values": {k: as_int(k, r4(v) if not isinstance(v, (str, bool)) and v is not None else v)
                   for k, v in last.items()},
        "reference_levels": {"above": above, "below": below, "notes": notes},
        "outlook": {k: r4(v) if isinstance(v, float) else v for k, v in ol.items()} if ol else None,
        "calibration_past_year": calibration(daily, since=year_start),
        "ex_dividends_last_60_days": [{"date": d, "amount": a} for d, a in zip(daily["date"], daily["dividend"])
                                      if a and d >= (date.fromisoformat(last["date"]) - timedelta(days=60)).isoformat()],
    }
    cutoff = (date.fromisoformat(last["date"]) - timedelta(days=history_days)).isoformat()
    hist = daily[daily["date"] > cutoff].reset_index(drop=True)
    wk = weeks[weeks["last_date"] > cutoff].reset_index(drop=True)
    return hist, wk, latest


def ledger_text(symbol, daily, weeks, latest):
    """A readable Appendix-B-style ledger of the latest day."""
    v = latest["values"]
    L = []
    f = lambda x, dp=4: "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.{dp}f}"
    L.append(f"INDICATOR LEDGER - {symbol} ({latest['market']}, {latest['currency']}) - official figures computed by the pipeline")
    L.append(f"as of {latest['as_of']} | last bar {latest['last_bar']} | expected session {latest['expected_session']} | "
             f"bars used {latest['bars_used']} | excluded {latest['bars_excluded'] or 'none'}")
    for name, cinfo in latest["comparators"].items():
        behind = cinfo["sessions_behind_stock"]
        L.append(f"comparator {name} ends {cinfo['last_date']}" + (f" -> {behind} session(s) before the stock" if behind else " (same as stock)"))
    exd = latest["ex_dividends_last_60_days"]
    L.append("ex-dates in last 60 days: " + (", ".join(f"{e['date']} {e['amount']}" for e in exd) if exd else "none"))
    L.append("")
    L.append("--- LAST 10 BARS ---")
    cols = ["date", "open", "high", "low", "close", "volume", "avg_vol20_prior", "clv", "vol_delta", "chg_raw_pct", "chg_pct", "gap_pct", "vol_ratio"]
    L.append(daily[cols].tail(10).to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    L.append(f"CVD10={f(v['cvd10'], 0)}")
    L.append(f"VWAP10 = {f(v['vwap10'])}; Close vs VWAP {f(v['close_vs_vwap10_pct'], 2)}%")
    L.append(f"Volume ratio = {f(v['volume'], 0)} / {f(v['avg_vol20_prior'], 1)} = {f(v['vol_ratio'])}")
    L.append(f"UDVR10 (adjusted classification): up {f(v['up_days10'], 0)} vol {f(v['up_vol10'], 0)} | down {f(v['down_days10'], 0)} "
             f"vol {f(v['down_vol10'], 0)} | ratio {f(v['udvr10'])} {v['udvr10_label']}")
    L.append(f"VT5/20 = {f(v['vt5_20'])} {v['vt5_20_label']}; 5-bar chg raw {f(v['chg5_raw_pct'])}% adj {f(v['chg5_pct'])}%")
    L.append(f"ATR14 (Wilder) = {f(v['atr14'])}; last 14 TR: " + ", ".join(f"{x:.2f}" for x in daily["true_range"].tail(14)))
    if len(daily) > 1:   # one-step check for an AI without code: (13 x previous ATR14 + latest TR) / 14
        pa, tr = daily["atr14"].iloc[-2], daily["true_range"].iloc[-1]
        L.append(f"ATR14 one-step check: previous ATR14 {f(pa)}; latest TR = max(H-L, |H-prevC|, |L-prevC|) = {f(tr)}; "
                 f"(13 x {f(pa)} + {f(tr)}) / 14 = {f((13 * pa + tr) / 14)}")
    L.append(f"Expected 5-day range = {f(v['close'])} +/- {f(v['atr14'])}*2.236 = {f(v['range5_low'])} - {f(v['range5_high'])}")
    prev_hist = daily["macd_hist"].iloc[-2] if len(daily) > 1 else None
    L.append(f"MACD={f(v['macd'])} Signal={f(v['macd_signal'])} Hist={f(v['macd_hist'])}; prev hist {f(prev_hist)} {v['macd_note']}")
    L.append(f"Momentum proxy (substitute only): SMA20 {f(v['sma20'])} -> {v['momentum_proxy']}")
    L.append(f"10-bar change raw {f(v['chg10_raw_pct'])}% adj {f(v['chg10_pct'])}%; Divergence: {v['divergence']}")
    L.append(f"Tenkan={f(v['tenkan'])} Kijun={f(v['kijun'])} SpanA={f(v['span_a'])} SpanB={f(v['span_b'])} | "
             f"Kumo {v['price_vs_kumo']} | TK {v['tk_state']} | Chikou {v['chikou']} | score {v['ichimoku_score']} -> {v['ichimoku_regime']}")
    if v.get("darvas_ceiling") is not None:
        L.append(f"Darvas: ceiling {f(v['darvas_ceiling'])} on {v['darvas_ceiling_date']}, floor {f(v['darvas_floor'])}, "
                 f"status {v['darvas_status']}, age {v['darvas_age_bars']} bars{' OLD BOX (context only)' if v['darvas_old_box'] else ''}"
                 + (f"; crossing day {v['darvas_crossing_date']} ({v['darvas_crossing_bars_ago']} bars ago) vol ratio "
                    f"{f(v['darvas_crossing_vol_ratio'])} {v['darvas_crossing_volume_label']}" if v.get("darvas_crossing_date") else ""))
    else:
        L.append("Darvas: INSUFFICIENT DATA (have %d, need 64)" % len(daily) if len(daily) < 64 else "Darvas: NO ACTIVE BOX")
    L.append(f"RS20: stock 20-bar change raw {f(v['chg20_raw_pct'])}% adj {f(v['chg20_pct'])}%")
    for name, ci in latest["comparators"].items():
        L.append(f"  {name}: {f(ci['chg20_pct'])}% ; RS20 = {f(ci['rs20_pts'])} pts {ci['label']}  [{name} to {ci['last_date']}]")
    if v.get("high52w") is None:
        L.append(f"52w high/low: INSUFFICIENT DATA (have {v['bars_in_52w_window']}, need 240)")
    else:
        L.append(f"52w: high {f(v['high52w'])} ({v['high52w_date']}) low {f(v['low52w'])} ({v['low52w_date']}); "
                 f"from high {f(v['from_high52w_pct'])}% from low {f(v['from_low52w_pct'])}% (bars in window {v['bars_in_52w_window']})")
    L.append(f"20-bar HH {f(v['hh20'])} LL {f(v['ll20'])}; 60-bar HH {f(v['hh60'])} LL {f(v['ll60'])}; 120-bar HH {f(v['hh120'])} LL {f(v['ll120'])}")
    rl = latest["reference_levels"]
    L.append("Levels above: " + (" | ".join(f"{m['name']} {f(m['price'])} ({m['dist_pct']:+.2f}%)" for m in rl["above"]) or "none"))
    L.append("Levels below: " + (" | ".join(f"{m['name']} {f(m['price'])} ({m['dist_pct']:+.2f}%)" for m in rl["below"]) or "none"))
    for n_ in rl["notes"]:
        L.append("  " + n_)
    L.append(f"Signals: VWAP {v['signal_vwap']} Ichimoku {v['signal_ichimoku']} MACD {v['signal_macd']} CVD {v['signal_cvd']} -> "
             f"{v['confluence']} ({v['bull_signals']} bull / {v['bear_signals']} bear of {v['signals_available']}); "
             f"Volume Confirmation {v['volume_confirmation']}; final {v['confluence_final']}")
    cal_ = latest["calibration_past_year"]
    L.append(f"Calibration (past year): 5d {cal_['5d_inside']}/{cal_['5d_windows']} = {cal_['5d_share_pct']}% ; "
             f"10d {cal_['10d_inside']}/{cal_['10d_windows']} = {cal_['10d_share_pct']}%")
    ol = latest["outlook"]
    if ol:
        L.append(f"Outlook: start day {ol['start_day']}; missing sessions after last bar {ol['missing_sessions'] or 'none'}; "
                 f"closures {ol['closures'] or 'none'}; early closes {ol['early_closes'] or 'none'}")
        L.append(f"  Week 1 (to {ol['week1_end']}) d={ol['d1']}: {f(v['close'])} +/- {f(v['atr14'])}*{ol['sqrt_d1']} -> {f(ol['week1_low'])} - {f(ol['week1_high'])}")
        L.append(f"  Week 2 cumulative (to {ol['week2_end']}) d={ol['d2']}: {f(v['close'])} +/- {f(v['atr14'])}*{ol['sqrt_d2']} -> {f(ol['week2_low'])} - {f(ol['week2_high'])}")
    if latest.get("extra_ledger"):
        L.append("")
        L += latest["extra_ledger"]
    L.append("")
    L.append("--- WEEKS (last 4) ---")
    for _, w in weeks.tail(4).iterrows():
        L.append(f"Week {w['week_of']} bars {w['bars']}{'' if w['complete'] else ' (incomplete)'}: O {w['open']} C {w['close']} H {w['high']} L {w['low']} "
                 f"prevC {w['prev_close']} chg raw {f(w['chg_raw_pct'])}% adj {f(w['chg_pct'])}% up {w['up_days']} dn {w['down_days']} "
                 f"volx {f(w['volume_vs_normal'])} CVD {f(w['week_cvd'], 0)} UD {f(w['up_down_vol_ratio'])} pos {f(w['close_position'])} "
                 f"standout {w['standout_date']} V {f(w['standout_volume'], 0)} chg {f(w['standout_chg_pct'])}% CLV {f(w['standout_clv'])}")
    return "\n".join(L)


COLUMNS_MD = """# Indicator columns (one row per trading day)

All figures follow the Individual Stock Harness v3.2 definitions and are computed from the
pipeline's Yahoo daily bars (real Close, not adjusted). `_pct` = percent; `_pts` = percentage points.
"adj" figures add back any dividend whose ex-date falls inside the window; the adjusted figure decides labels.

| Column | Plain English |
|---|---|
| date, open, high, low, close, volume | The day's bar |
| dividend | Dividend with its ex-date on this day (0 if none) |
| chg_raw_pct / chg_pct | Change vs the previous close; chg_pct adds back a dividend on its ex-date |
| gap_pct | Open vs the previous close |
| clv | Where the close sat in the day's range: -1 = at the low, +1 = at the high |
| vol_delta | Volume x CLV: an estimate of buying (+) or selling (-) pressure that day |
| cvd10 | Sum of vol_delta over the last 10 days |
| vwap10, close_vs_vwap10_pct | Volume-weighted average price of the last 10 days; how far the close is from it |
| avg_vol20_prior, vol_ratio | Average volume of the 20 days before; today's volume divided by it |
| udvr10 (+ label, up/down days and volumes) | Volume on up days / volume on down days, last 10 days. Above 1.1 buying-dominated, below 0.9 selling-dominated |
| vt5_20 (+ label) | Average volume of the last 5 days / the 20 days before them. Below 0.9 quieter, above 1.1 busier |
| chg5/chg10/chg20 (_raw_pct, _pct) | Price change over 5, 10, 20 bars |
| divergence | 10-day price change and cvd10 pointing opposite ways |
| true_range, atr14 | Daily range including gaps; ATR14 = its Wilder average (typical daily swing) |
| range5_low / range5_high | Close +/- ATR14 x 2.236: the heuristic 5-day range |
| macd, macd_signal, macd_hist | MACD 12/26/9 (exponential averages of the close) |
| sma20, momentum_proxy | 20-day average and the substitute momentum reading (used only without MACD) |
| hh20/ll20, hh60/ll60, hh120/ll120 | Highest high / lowest low of the last 20, 60, 120 bars |
| high52w, low52w (+ dates), from_high52w_pct, from_low52w_pct | 52-week high and low, and the distance from each |
| tenkan, kijun, span_a, span_b, kumo_top, kumo_bottom | Ichimoku lines and the cloud for this day |
| price_vs_kumo, tk_state, chikou, ichimoku_score, ichimoku_regime | Ichimoku readings: score from -3 (strong bearish) to +3 (strong bullish) |
| darvas_* | Latest Darvas box: ceiling, floor, status, age, OLD BOX flag (over 60 bars), crossing day and its volume ratio |
| rs20_<comparator>, chg20_<comparator>_pct | Stock's 20-day change minus the comparator's (points). rs20_main is the main benchmark |
| <comparator>_close_date | The comparator's bar used (earlier than the stock's date = the comparator lagged) |
| signal_vwap, signal_ichimoku, signal_macd, signal_cvd | The four confluence signals: +1 bullish, -1 bearish, 0 neutral |
| confluence, bull_signals, bear_signals, signals_available | Confluence verdict before the volume check |
| volume_confirmation, confluence_final | CONFIRMS / NEUTRAL / CONTRADICTS, and the verdict after any downgrade |
| outlook_d1/d2, outlook_week1_low/high, outlook_week2_low/high | That day's heuristic ranges for the next week and two weeks (holidays from holidays.txt) |
| nearest_support, nearest_resistance | Closest computed level below / above the close |

The weeks file has one row per Monday-to-Friday week: open, close, high, low, change (raw and adjusted),
up and down days, up/down volume, volume vs normal, week CVD, close position in range (0 = at the low,
1 = at the high), the standout (highest-volume) day, and each comparator's change and the relative move.

`<SYMBOL>_latest.json` and `<SYMBOL>_ledger.txt` add the latest day's reference levels, outlook,
range calibration, comparator dates and the SCORES section.

## Pillar readings file (`<SYMBOL>_scores.csv`, one row per day)

Statistics only, not advice: the readings behind the Individual Stock Harness pillars, counted. GitHub sets
no entries, stops, targets or trade labels; the AI harness does the trade reasoning.

| Column | Plain English |
|---|---|
| micro_bullish / micro_bearish / micro_label | Of 4 readings (close vs VWAP10, CVD10, UDVR10, divergence or volume trend): how many are bullish / bearish. Label BULLISH or BEARISH at 3 or more, else MIXED |
| momentum_bullish / momentum_bearish / momentum_label | Of 3 readings (Ichimoku, MACD, RS20 vs the main benchmark). Label BULLISH or BEARISH at 2 or more with neither Ichimoku nor MACD the other way, else MIXED |
| darvas_status, darvas_old_box | Latest Darvas box status; an OLD BOX (over 60 bars) is context only |
| support_dist_pct / _atr, resistance_dist_pct / _atr | Distance from the close to the nearest computed support / resistance, in % and in ATRs |
| ratings_upgrades_14d / ratings_downgrades_14d | Broker rating changes in the news store over the last 14 days (initiations at buy or sell count as up or down) |

Trend charts: see the stock pages on the dashboard.
"""


def write_outputs(symbol, daily, weeks, latest, outdir=OUTDIR):
    outdir.mkdir(parents=True, exist_ok=True)
    stem = symbol.replace("^", "")
    d = daily.copy()
    for col in d.columns:
        if col in INT_COLS:
            d[col] = d[col].astype("Int64")
        elif d[col].dtype.kind == "f":
            d[col] = d[col].round(4)
    d.to_csv(outdir / f"{stem}.csv", index=False)
    w = weeks.copy()
    for col in w.columns:
        if w[col].dtype.kind == "f":
            w[col] = w[col].round(4)
    w.to_csv(outdir / f"{stem}_weeks.csv", index=False)
    (outdir / f"{stem}_latest.json").write_text(json.dumps({k: v for k, v in latest.items() if k != "extra_ledger"},
                                                           indent=1, default=str), encoding="utf-8")
    (outdir / f"{stem}_ledger.txt").write_text(ledger_text(symbol, daily, weeks, latest) + "\n", encoding="utf-8")
    (outdir / "COLUMNS.md").write_text(COLUMNS_MD, encoding="utf-8")


if __name__ == "__main__":
    sym = sys.argv[1]
    mk = "SGX" if sym.endswith(".SI") else "US"
    prices = DATA / "prices"
    bars = pd.read_csv(prices / f"{sym.replace('^', '')}.csv")
    dv = DATA / "dividends" / f"{sym}.csv"
    divs = pd.read_csv(dv) if dv.exists() else None
    bench = ["^STI"] if mk == "SGX" else ["^GSPC", "^IXIC", "^DJI"]
    comps = {b: pd.read_csv(prices / f"{b.replace('^', '')}.csv") for b in bench}
    daily, weeks, latest = build(sym, bars, divs, comps, bench[0], mk, date.today().isoformat(), None, history_days=10000)
    print(ledger_text(sym, daily, weeks, latest))
