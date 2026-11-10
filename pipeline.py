"""
Daily stock data pipeline.

Runs once a day on GitHub Actions (see .github/workflows/daily.yml) and writes
everything into docs/data/, which GitHub Pages publishes:

  prices/<SYMBOL>.csv     one year of daily bars (real Close, not adjusted)
  dividends/<SYMBOL>.csv  dividends and splits, last two years
  data_pack.txt           figures for the Stock Selection Harness
  news_pack.txt           last 30 days of news for both harnesses
  status.txt              what worked and what didn't on this run
  summary.md              short market summary (used in the daily email)
  history/<date>/         dated copy of status, summary, data pack, news pack
  ai_bundle.txt           status + data pack + news pack in one paste
  dashboard.json          what the dashboard page reads
  news_store.json         the script's own memory of news it has seen

Rules it follows:
  * Every figure carries its source, a link and a date.
  * Anything it can't get says NOT RETRIEVED. It never guesses.
  * If prices or the 1-year rates fail, the run ends with an error so GitHub
    emails you. Missing fundamentals or news summaries don't count as errors.
"""

import io
import json
import math
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
WATCHLIST = ROOT / "watchlist.txt"
OUT = ROOT / "docs" / "data"
PRICES = OUT / "prices"
DIVS = OUT / "dividends"
ARCHIVE = OUT / "archive"   # files of names taken off the watchlist

SGT = timezone(timedelta(hours=8))
NOW = datetime.now(SGT)
TODAY = NOW.date()
TODAY_S = TODAY.isoformat()
RUN_STAMP = NOW.strftime("%Y-%m-%d %H:%M SGT")

BENCHMARKS = [
    ("^STI", "Straits Times Index"),
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq Composite"),
    ("^DJI", "Dow Jones Industrial Average"),
]
NEWS_DAYS = 30            # how much news history to keep
NEWS_PER_TICKER = 25      # newest Google News items read per ticker per run
SUMMARY_WORDS = 50        # cap on stored preview text (public repo)
SUMMARY_FETCH_LIMIT = 150  # max article pages opened per run (keeps run short)
STALE_DAYS = 5            # warn if a ticker's latest bar is older than this

NR = "NOT RETRIEVED"
NO_SUMMARY = "SUMMARY NOT RETRIEVED - open the link before relying on the headline"
UA = {"User-Agent": "Mozilla/5.0 (compatible; personal-stock-data-pipeline/1.0)"}
YAHOO_SRC = "Yahoo Finance via yfinance"

PRIMARY_SOURCES = [
    "business wire", "pr newswire", "prnewswire", "globenewswire", "globe newswire",
    "accesswire", "sgx", "sec.gov", "u.s. securities and exchange commission",
]

# ----------------------------------------------------------------------------
# Run report: collects problems for status.txt and decides the exit code
# ----------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.critical = []   # missing prices or rates -> run fails -> email
        self.warnings = []   # expected gaps -> listed, no email
        self.ok = []

    def crit(self, msg):
        print("CRITICAL:", msg)
        self.critical.append(msg)

    def warn(self, msg):
        print("WARNING:", msg)
        self.warnings.append(msg)

    def good(self, msg):
        print("OK:", msg)
        self.ok.append(msg)

    def text(self):
        state = "FAILED - see CRITICAL below" if self.critical else (
            "OK with gaps" if self.warnings else "OK")
        lines = [f"RUN STATUS: {state}", f"Run time: {RUN_STAMP}", ""]
        lines.append(f"CRITICAL ({len(self.critical)}): these gaps break the harnesses")
        lines += [f"  - {m}" for m in self.critical] or ["  - none"]
        lines.append("")
        lines.append(f"GAPS ({len(self.warnings)}): expected, marked {NR} in the files")
        lines += [f"  - {m}" for m in self.warnings] or ["  - none"]
        return "\n".join(lines)


R = Report()

# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def clean(x):
    """Make text safe for a pipe-separated line."""
    s = "" if x is None else str(x)
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace("|", "/")


def line(*fields):
    return " | ".join(clean(f) for f in fields)


def num(x):
    """Return a float, or None if missing/not a number."""
    try:
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def fmt(x, dp=2, suffix=""):
    f = num(x)
    return NR if f is None else f"{f:,.{dp}f}{suffix}"


def safe_name(symbol):
    return symbol.replace("^", "")


def yurl(symbol, page=""):
    return f"https://finance.yahoo.com/quote/{symbol}/{page}"


def get(url, **kw):
    kw.setdefault("headers", UA)
    kw.setdefault("timeout", 25)
    r = requests.get(url, **kw)
    r.raise_for_status()
    return r


def to_date(x):
    """Turn the many date shapes Yahoo returns into YYYY-MM-DD, or None."""
    if x is None:
        return None
    try:
        if isinstance(x, (int, float)):
            if x <= 0:
                return None
            return datetime.fromtimestamp(x, tz=timezone.utc).astimezone(SGT).date().isoformat()
        ts = pd.Timestamp(x)
        if ts.tzinfo is not None:
            ts = ts.tz_convert(SGT)
        return ts.date().isoformat()
    except Exception:
        return None


def pages_base():
    """Public link prefix for the files, e.g. https://name.github.io/stock-data/data/"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/data/"
    return "data/"


# ----------------------------------------------------------------------------
# Watchlist
# ----------------------------------------------------------------------------
def read_watchlist(path=WATCHLIST):
    items = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split("|")]
        sym = parts[0].upper()
        name = parts[1] if len(parts) > 1 and parts[1] else sym
        kind = (parts[2].lower() if len(parts) > 2 and parts[2] else "company")
        if kind not in ("company", "bank", "insurer", "reit"):
            R.warn(f"{sym}: type '{kind}' not recognised, treated as company")
            kind = "company"
        query = parts[3] if len(parts) > 3 and parts[3] else f'"{name}"'
        items.append({"symbol": sym, "name": name, "kind": kind, "query": query})
    if len(items) > 15:
        R.warn(f"Watchlist has {len(items)} names; the Stock Selection Harness takes 15 at most")
    return items


# ----------------------------------------------------------------------------
# Yahoo Finance
# ----------------------------------------------------------------------------
def yf_ticker(symbol):
    import yfinance as yf  # imported here so the rest can be tested offline
    return yf.Ticker(symbol)


def fetch_history(tk, symbol, tries=3):
    last = None
    for i in range(tries):
        try:
            df = tk.history(period="5y", interval="1d", auto_adjust=False, actions=False)
            if df is not None and not df.empty and "Close" in df.columns:
                return df
            last = "empty result"
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"no price data from Yahoo after {tries} tries ({last})")


def write_prices(symbol, hist):
    """One year of daily bars. Real Close; Adj Close is left out on purpose."""
    df = pd.DataFrame({
        "Date": hist.index.strftime("%Y-%m-%d"),
        "Open": hist["Open"].values,
        "High": hist["High"].values,
        "Low": hist["Low"].values,
        "Close": hist["Close"].values,
        "Volume": hist["Volume"].values,
    }).dropna(subset=["Close"])
    cutoff = (TODAY - timedelta(days=365)).isoformat()
    df = df[df["Date"] >= cutoff].copy()
    for c in ("Open", "High", "Low", "Close"):
        df[c] = df[c].round(4)
    df["Volume"] = df["Volume"].fillna(0).astype("int64")
    PRICES.mkdir(parents=True, exist_ok=True)
    df.to_csv(PRICES / f"{safe_name(symbol)}.csv", index=False)
    return df


def write_dividends(symbol, tk):
    rows = []
    cutoff = pd.Timestamp(TODAY - timedelta(days=730))
    for kind, series in (("Dividend", getattr(tk, "dividends", None)),
                         ("Split", getattr(tk, "splits", None))):
        if series is None or len(series) == 0:
            continue
        for d, v in series.items():
            ts = pd.Timestamp(d).tz_localize(None) if pd.Timestamp(d).tzinfo else pd.Timestamp(d)
            if ts >= cutoff and num(v):
                rows.append({"Date": ts.date().isoformat(), "Type": kind, "Amount": round(float(v), 6)})
    df = pd.DataFrame(rows, columns=["Date", "Type", "Amount"]).sort_values("Date")
    DIVS.mkdir(parents=True, exist_ok=True)
    df.to_csv(DIVS / f"{safe_name(symbol)}.csv", index=False)
    return df


def safe_attr(tk, name):
    try:
        return getattr(tk, name)
    except Exception:  # noqa: BLE001
        return None


def stmt_values(frame, row_names):
    """[(Timestamp, value)] from a yfinance statement for the first row that exists."""
    if frame is None or getattr(frame, "empty", True):
        return []
    for rn in row_names:
        if rn in frame.index:
            out = []
            for col, v in frame.loc[rn].items():
                f = num(v)
                if f is not None:
                    out.append((pd.Timestamp(col).tz_localize(None) if pd.Timestamp(col).tzinfo
                                else pd.Timestamp(col), f))
            return sorted(out)
    return []


def ratio_range(tk, hist, kind, info):
    """
    Approximate 5-year high / median / low of P/E (companies) or P/B, P/NAV
    (banks, insurers, REITs): each month-end close divided by the latest
    annual figure published at least 60 days earlier.
    Returns (label, dict or None, note).
    """
    label = {"company": "P/E", "reit": "P/NAV"}.get(kind, "P/B")
    pc, fc = info.get("currency"), info.get("financialCurrency")
    if pc and fc and pc != fc:
        return label, None, f"accounts are in {fc} but the price is in {pc}, so the ratio can't be built from this feed"

    if kind == "company":
        metric = stmt_values(safe_attr(tk, "income_stmt"), ["Diluted EPS", "Basic EPS"])
        basis = "latest annual diluted EPS"
    else:
        bs = safe_attr(tk, "balance_sheet")
        eq = dict(stmt_values(bs, ["Common Stock Equity", "Stockholders Equity"]))
        sh = dict(stmt_values(bs, ["Ordinary Shares Number", "Share Issued"]))
        metric = sorted((d, eq[d] / sh[d]) for d in eq if d in sh and sh[d])
        basis = "latest annual book value (NAV) per share"
    metric = [(d, v) for d, v in metric if v > 0]
    if not metric:
        return label, None, "no positive annual figures in Yahoo's statements"

    closes = hist["Close"].copy()
    closes.index = pd.DatetimeIndex(closes.index).tz_localize(None)
    closes = closes.dropna()
    grp = closes.groupby(closes.index.to_period("M"))
    # last real trading day of each month (not the calendar month-end)
    monthly = pd.Series(grp.last().values, index=grp.apply(lambda s: s.index[-1]).values)
    ratios = []
    for mdate, px in monthly.items():
        avail = [(d, v) for d, v in metric if d + pd.Timedelta(days=60) <= mdate]
        if avail:
            ratios.append((mdate, px / max(avail)[1]))
    if len(ratios) < 6:
        return label, None, f"only {len(ratios)} months could be matched to published figures"
    s = pd.Series([r for _, r in ratios])
    return label, {
        "high": s.max(), "median": s.median(), "low": s.min(), "months": len(s),
        "from": ratios[0][0].date().isoformat(), "to": ratios[-1][0].date().isoformat(),
        "basis": basis,
    }, ""


def stock_figures(item, tk, hist, prices, divs):
    """Data-pack lines for one stock."""
    sym, kind = item["symbol"], item["kind"]
    out = []
    retrieved = f"retrieved {TODAY_S}"

    try:
        info = tk.info or {}
    except Exception as e:  # noqa: BLE001
        info = {}
        R.warn(f"{sym}: Yahoo company details unavailable ({e.__class__.__name__})")
    ccy = info.get("currency") or ""

    def add(fig, value, definition, url, d):
        out.append(line(f"{sym} {fig}", value, definition, YAHOO_SRC, url, d))

    # Price and liquidity
    last = prices.iloc[-1]
    add("latest close", f"{last['Close']:.4f} {ccy}".strip(), "Closing price of the latest daily bar (not adjusted)",
        yurl(sym, "history/"), last["Date"])
    vol3m = prices["Volume"].tail(63).mean()
    add("3-month average daily volume", f"{vol3m:,.0f} shares",
        f"Mean daily volume over the last {min(63, len(prices))} trading days, computed from daily bars",
        yurl(sym, "history/"), last["Date"])
    if len(prices) < 240:
        R.warn(f"{sym}: only {len(prices)} daily bars (less than a year of history)")

    # Sector
    sector = info.get("sector")
    add("sector", sector or NR,
        "Yahoo Finance sector classification (close to, but not the same as, GICS)",
        yurl(sym, "profile/"), retrieved)

    # EPS
    mismatch = bool(info.get("financialCurrency") and ccy and info["financialCurrency"] != ccy)
    eps_unit = " (currency: see note)" if mismatch else f" {ccy}"
    add("trailing EPS", fmt(info.get("trailingEps"), 4, eps_unit), "Earnings per share, last 12 months",
        yurl(sym, "key-statistics/"), retrieved)
    add("forward EPS", fmt(info.get("forwardEps"), 4, eps_unit), "Analyst consensus EPS for the next fiscal year",
        yurl(sym, "key-statistics/"), retrieved)
    if mismatch:
        add("currency note", f"accounts in {info['financialCurrency']}, price in {ccy}",
            "EPS and book value may be in the accounting currency; check before using ratios",
            yurl(sym, "financials/"), retrieved)

    # Book value / NAV
    if kind in ("bank", "insurer"):
        add("book value per share", fmt(info.get("bookValue"), 4, f" {ccy}"),
            "Shareholders' equity per share, latest reported", yurl(sym, "key-statistics/"), retrieved)
    if kind == "reit":
        add("NAV per unit", fmt(info.get("bookValue"), 4, f" {ccy}"),
            "Book value per unit as reported to Yahoo (proxy for NAV per unit)", yurl(sym, "key-statistics/"), retrieved)

    # Valuation now
    if kind == "company":
        add("trailing P/E", fmt(info.get("trailingPE")), "Price / trailing 12-month EPS", yurl(sym, "key-statistics/"), retrieved)
        add("forward P/E", fmt(info.get("forwardPE")), "Price / forward EPS", yurl(sym, "key-statistics/"), retrieved)
    else:
        add("P/B" if kind != "reit" else "P/NAV", fmt(info.get("priceToBook")),
            "Price / book value per share", yurl(sym, "key-statistics/"), retrieved)

    # Valuation history
    try:
        label, rr, note = ratio_range(tk, hist, kind, info)
    except Exception as e:  # noqa: BLE001
        label, rr, note = ("P/E" if kind == "company" else "P/B"), None, f"calculation error ({e.__class__.__name__})"
    if rr:
        span = f"{rr['months']} month-ends, {rr['from']} to {rr['to']}"
        why = []
        if rr["months"] < 36:
            why.append(f"covers only {rr['months']} months")
        if rr["low"] > 0 and rr["high"] / rr["low"] > 4:
            why.append(f"high is {rr['high'] / rr['low']:.0f}x the low, so earnings or book value changed sharply")
        if label == "P/E" and rr["high"] > 100:
            why.append("high above 100 means earnings were near zero in part of the period")
        flag = f"UNRELIABLE ({'; '.join(why)}). " if why else ""
        for k in ("high", "median", "low"):
            add(f"5-year {label} {k}", f"{rr[k]:.2f}",
                f"{flag}Approximate: month-end close / {rr['basis']}; covers {span}",
                yurl(sym, "financials/"), retrieved)
        if why:
            R.warn(f"{sym}: 5-year {label} range marked UNRELIABLE ({'; '.join(why)})")
        elif rr["months"] < 54:
            R.warn(f"{sym}: {label} range covers only {rr['months']} months, not 5 years")
    else:
        for k in ("high", "median", "low"):
            add(f"5-year {label} {k}", NR, note, yurl(sym, "financials/"), retrieved)
        R.warn(f"{sym}: 5-year {label} range - {note}")

    # Dividends (trailing 12 months, from the dividend history)
    one_year = (TODAY - timedelta(days=365)).isoformat()
    d12 = divs[(divs["Type"] == "Dividend") & (divs["Date"] >= one_year)]["Amount"].sum() if len(divs) else 0.0
    name = "distribution per unit (DPU), trailing 12 months" if kind == "reit" else "dividend per share, trailing 12 months"
    add(name, f"{d12:.4f} {ccy}".strip(),
        f"Sum of cash dividends with ex-dates since {one_year}", yurl(sym, "history/?filter=div"), TODAY_S)

    # Balance-sheet strength
    if kind == "company":
        debt, cash, ebitda = num(info.get("totalDebt")), num(info.get("totalCash")), num(info.get("ebitda"))
        if None not in (debt, cash, ebitda) and ebitda > 0:
            add("net debt/EBITDA", f"{(debt - cash) / ebitda:.2f}x",
                "(Total debt - cash) / EBITDA, last 12 months; negative = net cash", yurl(sym, "key-statistics/"), retrieved)
        else:
            add("net debt/EBITDA", NR, "Debt, cash or EBITDA missing (or EBITDA not positive)", yurl(sym, "key-statistics/"), retrieved)
            R.warn(f"{sym}: net debt/EBITDA not available")
    elif kind == "bank":
        add("CET1 ratio", NR, "Not carried by Yahoo; see the bank's quarterly Pillar 3 disclosure", yurl(sym, "profile/"), retrieved)
        add("CET1 regulatory minimum", NR, "Set by the bank's regulator; add from the Pillar 3 disclosure", yurl(sym, "profile/"), retrieved)
        R.warn(f"{sym}: CET1 and its minimum need the bank's own disclosure")
    elif kind == "reit":
        bs = safe_attr(tk, "balance_sheet")
        td = stmt_values(bs, ["Total Debt"])
        ta = stmt_values(bs, ["Total Assets"])
        if td and ta and ta[-1][1]:
            add("gearing", f"{td[-1][1] / ta[-1][1] * 100:.1f}%", "Total debt / total assets, latest annual balance sheet",
                yurl(sym, "balance-sheet/"), ta[-1][0].date().isoformat())
        else:
            add("gearing", NR, "Debt or assets missing from Yahoo's balance sheet", yurl(sym, "balance-sheet/"), retrieved)
            R.warn(f"{sym}: gearing not available")
    elif kind == "insurer":
        add("solvency ratio", NR, "Not carried by Yahoo; see the insurer's own capital disclosure", yurl(sym, "profile/"), retrieved)
        R.warn(f"{sym}: solvency ratio needs the insurer's own disclosure")

    # Dates
    cal = safe_attr(tk, "calendar") or {}
    if not isinstance(cal, dict):
        try:
            cal = cal.to_dict()
        except Exception:  # noqa: BLE001
            cal = {}
    ed = cal.get("Earnings Date")
    ed = ed[0] if isinstance(ed, (list, tuple)) and ed else ed
    ed_s = to_date(ed)
    if ed_s and ed_s >= TODAY_S:
        add("next results date", ed_s,
            "Next earnings date per Yahoo (estimated if the company hasn't confirmed)", yurl(sym), retrieved)
    elif ed_s:
        # Yahoo keeps showing the last date until the next one is announced
        add("next results date", NR,
            f"Not yet announced; Yahoo still shows the most recent results date ({ed_s})", yurl(sym), retrieved)
        add("most recent results date", ed_s, "Last earnings release per Yahoo", yurl(sym), retrieved)
        R.warn(f"{sym}: next results date not yet announced (last was {ed_s})")
    else:
        add("next results date", NR, "No earnings date reported by Yahoo", yurl(sym), retrieved)
        R.warn(f"{sym}: next results date not available")
    xd = to_date(cal.get("Ex-Dividend Date")) or to_date(info.get("exDividendDate"))
    if xd:
        when = "upcoming" if xd >= TODAY_S else "most recent (no future date announced yet)"
        add("ex-dividend date", xd, f"Ex-dividend date per Yahoo - {when}", yurl(sym), retrieved)
    else:
        add("ex-dividend date", NR, "No ex-dividend date reported", yurl(sym), retrieved)

    return out, info


# ----------------------------------------------------------------------------
# Rates and macro (official sources)
# ----------------------------------------------------------------------------
def get_retry(url, tries=3, timeout=60, **kw):
    last = None
    for i in range(tries):
        try:
            return get(url, timeout=timeout, **kw)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(10 * (i + 1))
    raise RuntimeError(f"no response after {tries} tries ({last.__class__.__name__})")


def treasury_1y():
    """Latest 1-year par yield from the US Treasury's own daily yield-curve file."""
    page = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"
    for year in (TODAY.year, TODAY.year - 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value={year}&page&_format=csv")
        df = pd.read_csv(io.StringIO(get_retry(url).text))
        if "1 Yr" in df.columns and not df.empty:
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.dropna(subset=["1 Yr"]).sort_values("Date")
            if not df.empty:
                return float(df["1 Yr"].iloc[-1]), df["Date"].iloc[-1].date().isoformat(), page
    raise RuntimeError("no 1-year column in the Treasury file")


def bls_series(series_id):
    """Monthly series from the BLS public API (no key needed) as a pandas Series."""
    url = f"https://api.bls.gov/publicAPI/v1/timeseries/data/{series_id}"
    js = get_retry(url).json()
    if js.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError("; ".join(js.get("message") or ["BLS request refused"]))
    pts = {}
    for d in js["Results"]["series"][0]["data"]:
        if d["period"].startswith("M") and d["period"] != "M13" and num(d["value"]) is not None:
            pts[pd.Timestamp(int(d["year"]), int(d["period"][1:]), 1)] = float(d["value"])
    s = pd.Series(pts).sort_index()
    if len(s) < 14:
        raise RuntimeError(f"only {len(s)} months returned")
    return s


def fred(series_id, tries=3):
    """Daily/monthly/quarterly series from FRED (St. Louis Fed) as a pandas Series."""
    r = get_retry(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", tries=tries)
    df = pd.read_csv(io.StringIO(r.text))
    s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values,
                  index=pd.to_datetime(df.iloc[:, 0])).dropna()
    if s.empty:
        raise RuntimeError("empty series")
    return s


def fred_url(series_id):
    return f"https://fred.stlouisfed.org/series/{series_id}"


def yoy_last_two(s):
    y = (s / s.shift(12) - 1) * 100
    y = y.dropna()
    return y.iloc[-1], y.index[-1], y.iloc[-2], y.index[-2]


def dbnomics(series_path):
    """
    One series from DBnomics (a free public mirror of official statistics, no key).
    series_path like "BEA/NIPA-T10101/A191RL-Q". Returns (pandas Series, web page URL).
    """
    api = f"https://api.db.nomics.world/v22/series/{series_path}?observations=1&format=json"
    js = get_retry(api, tries=2, timeout=40).json()
    docs = (js.get("series") or {}).get("docs") or []
    if not docs:
        raise RuntimeError(f"series {series_path} not found")
    d = docs[0]
    pts = {}
    for p, v in zip(d.get("period", []), d.get("value", [])):
        f = num(v)
        if f is None:
            continue
        m = re.match(r"^(\d{4})-Q([1-4])$", str(p))
        if m:   # quarter -> first day of the quarter
            pts[pd.Timestamp(int(m.group(1)), 3 * int(m.group(2)) - 2, 1)] = f
    s = pd.Series(pts).sort_index()
    if len(s) < 6:
        raise RuntimeError(f"only {len(s)} quarters in {series_path}")
    return s, f"https://db.nomics.world/{series_path}"


def qlabel(ts):
    return f"Q{(ts.month - 1) // 3 + 1} {ts.year}"


def us_gdp():
    """
    US real GDP growth from BEA tables, read through DBnomics.
    Returns a list of (figure, value, definition, source, url, date).
    """
    out = []
    src = "U.S. Bureau of Economic Analysis (BEA), via DBnomics"

    # Year-on-year: BEA table 1.1.11 directly; otherwise compute it from the
    # real GDP level in table 1.1.6, which is how BEA itself calculates it.
    yoy, how = None, ""
    try:
        yoy, url = dbnomics("BEA/NIPA-T10111/A191RO-Q")
        how = "BEA table 1.1.11"
    except Exception:  # noqa: BLE001
        lvl, url = dbnomics("BEA/NIPA-T10106/A191RX-Q")
        yoy = ((lvl / lvl.shift(4) - 1) * 100).dropna()
        how = "computed from BEA table 1.1.6 (real GDP, chained dollars): this quarter / same quarter a year earlier - 1"
    for i, tag in ((-1, "latest"), (-2, "prior")):
        q = yoy.index[i]
        out.append((f"US real GDP growth y/y ({tag})", f"{yoy.iloc[i]:.1f}%",
                    f"Change from the same quarter a year earlier, {qlabel(q)}; {how}", src, url, q.date().isoformat()))

    # Headline figure BEA announces: annualised quarter-on-quarter (table 1.1.1)
    try:
        qoq, url = dbnomics("BEA/NIPA-T10101/A191RL-Q")
        for i, tag in ((-1, "latest"), (-2, "prior")):
            q = qoq.index[i]
            out.append((f"US real GDP growth q/q annualised ({tag})", f"{qoq.iloc[i]:.1f}%",
                        f"Change from the preceding quarter at an annual rate, seasonally adjusted, {qlabel(q)}; BEA table 1.1.1 (the headline figure)",
                        src, url, q.date().isoformat()))
    except Exception as e:  # noqa: BLE001
        R.warn(f"US GDP q/q annualised: {e}")
    return out


def mas_tbill_1y():
    base = "https://eservices.mas.gov.sg/statistics/api/v1/bondsandbills/m/listbondsandbills"
    last = None
    for tenor in ("1", "1.0"):
        params = {
            "rows": 1, "sort": "auction_date desc",
            "filters": f'bill_bond_ind:"bill" AND product_type:"B" AND auction_tenor:"{tenor}" AND total_bids:[0.001 TO *]',
        }
        try:
            recs = get(base, params=params).json()["result"]["records"]
            if recs and num(recs[0].get("cutoff_yield")) is not None:
                return recs[0], base
            last = "no records"
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"MAS bonds-and-bills API gave no 1-year T-bill ({last})")


def singstat_row(table_id, row_prefix):
    url = f"https://tablebuilder.singstat.gov.sg/api/table/tabledata/{table_id}"
    data = get(url).json()
    rows = (data.get("Data") or {}).get("row") or []
    for row in rows:
        if str(row.get("rowText", "")).lower().startswith(row_prefix.lower()):
            pts = {}
            for c in row.get("columns", []):
                v = num(c.get("value"))
                if v is not None:
                    try:
                        pts[pd.to_datetime(c["key"], format="%Y %b")] = v
                    except Exception:  # noqa: BLE001
                        pass
            s = pd.Series(pts).sort_index()
            if not s.empty:
                return s, f"https://tablebuilder.singstat.gov.sg/table/TS/{table_id}"
    raise RuntimeError(f"row '{row_prefix}' not found in SingStat table {table_id}")


def market_figures():
    out = []

    def add(fig, value, definition, source, url, d):
        out.append(line(fig, value, definition, source, url, d))

    def missing(fig, why, source, url, critical=False):
        add(fig, NR, why, source, url, TODAY_S)
        (R.crit if critical else R.warn)(f"{fig}: {why}")

    # Risk-free rates (critical). US: Treasury first, FRED as back-up.
    try:
        v, d, url = treasury_1y()
        add("US 1-year Treasury yield", f"{v:.2f}%",
            "1-year par yield (constant maturity), daily Treasury par yield curve",
            "U.S. Department of the Treasury", url, d)
    except Exception as e1:  # noqa: BLE001
        try:
            s = fred("DGS1")
            add("US 1-year Treasury yield", f"{s.iloc[-1]:.2f}%",
                "1-year constant-maturity Treasury yield (Federal Reserve H.15)",
                "FRED, Federal Reserve Bank of St. Louis", fred_url("DGS1"), s.index[-1].date().isoformat())
            R.warn(f"US 1-year yield: Treasury file failed ({e1}); used FRED instead")
        except Exception as e2:  # noqa: BLE001
            missing("US 1-year Treasury yield", f"Treasury ({e1}) and FRED ({e2}) both failed",
                    "U.S. Department of the Treasury", "https://home.treasury.gov/", critical=True)

    try:
        rec, url = mas_tbill_1y()
        add("Singapore 1-year SGS T-bill yield", f"{float(rec['cutoff_yield']):.2f}%",
            f"Cut-off yield at the latest 1-year T-bill auction (issue {rec.get('issue_code', '?')})",
            "Monetary Authority of Singapore", url, to_date(rec.get("auction_date")) or TODAY_S)
    except Exception as e:  # noqa: BLE001
        missing("Singapore 1-year SGS T-bill yield", f"fetch failed ({e})", "MAS",
                "https://www.mas.gov.sg/bonds-and-bills", critical=True)

    # US macro
    try:
        for row in us_gdp():
            add(*row)
    except Exception as e:  # noqa: BLE001
        missing("US real GDP growth y/y", f"fetch failed ({e})", "BEA via DBnomics",
                "https://db.nomics.world/BEA/NIPA-T10106/A191RX-Q")

    # US CPI straight from BLS (not seasonally adjusted, the basis of the headline y/y figure)
    for sid, label, what in (("CUUR0000SA0", "US headline CPI", "CPI-U all items"),
                             ("CUUR0000SA0L1E", "US core CPI", "CPI-U all items less food and energy")):
        url = f"https://data.bls.gov/timeseries/{sid}"
        try:
            v1, d1, v0, d0 = yoy_last_two(bls_series(sid))
            add(f"{label} (latest)", f"{v1:.1f}%", f"{what}, year-on-year, {d1:%b %Y}", "U.S. Bureau of Labor Statistics", url, d1.date().isoformat())
            add(f"{label} (prior)", f"{v0:.1f}%", f"{what}, year-on-year, {d0:%b %Y}", "U.S. Bureau of Labor Statistics", url, d0.date().isoformat())
        except Exception as e:  # noqa: BLE001
            missing(label, f"fetch failed ({e})", "U.S. Bureau of Labor Statistics", url)

    missing("US manufacturing PMI (ISM)", "ISM data is licensed and not on a free feed; add from the ISM release",
            "ISM", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/")

    # Singapore macro
    try:
        s, url = singstat_row("M213751", "All Items")
        v1, d1, v0, d0 = yoy_last_two(s)
        add("Singapore headline CPI (latest)", f"{v1:.1f}%", f"Year-on-year change, {d1:%b %Y}", "SingStat", url, d1.date().isoformat())
        add("Singapore headline CPI (prior)", f"{v0:.1f}%", f"Year-on-year change, {d0:%b %Y}", "SingStat", url, d0.date().isoformat())
    except Exception as e:  # noqa: BLE001
        missing("Singapore headline CPI", f"fetch failed ({e})", "SingStat", "https://tablebuilder.singstat.gov.sg/table/TS/M213751")

    missing("Singapore MAS core inflation", "feed not yet connected; see the MAS core inflation page",
            "MAS", "https://www.mas.gov.sg/statistics/mas-core-inflation")
    missing("Singapore real GDP growth", "feed not yet connected; see SingStat GDP releases",
            "SingStat / MTI", "https://www.singstat.gov.sg/find-data/search-by-theme/economy/national-accounts/latest-data")
    missing("Singapore manufacturing PMI (SIPMM)", "published as a press release, not a data feed",
            "SIPMM", "https://www.sipmm.org.sg/")
    return out


# ----------------------------------------------------------------------------
# News
# ----------------------------------------------------------------------------
STOP = set("the a an and or of to in on for with at by from as is are be was were its it this that after over into amid".split())


def words(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) >= 3 and w not in STOP}


def similar(a, b):
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def cap_words(text, n=SUMMARY_WORDS):
    w = clean(text).split()
    return " ".join(w[:n]) + (" ..." if len(w) > n else "")


def google_news(item):
    sg = item["symbol"].endswith(".SI")
    hl, gl, ceid = ("en-SG", "SG", "SG:en") if sg else ("en-US", "US", "US:en")
    q = f"{item['query']} when:{NEWS_DAYS}d"
    url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl={hl}&gl={gl}&ceid={ceid}"
    root = ET.fromstring(get(url).content)
    found = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(SGT).date().isoformat()
        except Exception:  # noqa: BLE001
            continue
        found.append({"ticker": item["symbol"], "date": pub, "title": title, "source": source or "Unknown",
                      "url": (it.findtext("link") or "").strip(), "summary": None, "via": "google"})
    found.sort(key=lambda x: x["date"], reverse=True)
    return found[:NEWS_PER_TICKER]


def bing_queries(item):
    """Three angles per stock, so more items arrive with a summary."""
    name = item["name"]
    return [item["query"], f'"{name}" shares OR stock', f'"{name}" results OR earnings OR dividend OR guidance']


def bing_news(item):
    """Run every Bing angle for one stock; one failing angle doesn't stop the others."""
    found, errors = [], []
    for q in bing_queries(item):
        try:
            found += bing_feed(item, q)
        except Exception as e:  # noqa: BLE001
            errors.append(e.__class__.__name__)
        time.sleep(1)
    if errors and not found:
        raise RuntimeError(", ".join(errors))
    return found


def bing_feed(item, query):
    """
    Bing News RSS: gives the article's own link and a short description written
    from the article, so most items arrive with a summary already.
    """
    from urllib.parse import urlparse, parse_qs
    sg = item["symbol"].endswith(".SI")
    mkt = "en-SG" if sg else "en-US"
    url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss&mkt={mkt}&setlang=en"
    root = ET.fromstring(get(url).content)
    cutoff = (TODAY - timedelta(days=NEWS_DAYS)).isoformat()
    found = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        q = parse_qs(urlparse(link).query)
        if "url" in q:                      # Bing wraps the real link in a redirect
            link = q["url"][0]
        source = ""
        for child in it:
            if child.tag.split("}")[-1].lower() == "source" and child.text:
                source = child.text.strip()
        if not source:
            source = urlparse(link).netloc.replace("www.", "") or "Unknown"
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(SGT).date().isoformat()
        except Exception:  # noqa: BLE001
            continue
        if not title or pub < cutoff:
            continue
        desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
        found.append({"ticker": item["symbol"], "date": pub, "title": title, "source": source,
                      "url": link, "summary": cap_words(desc) if len(desc.split()) >= 6 else None, "via": "bing"})
    return found[:NEWS_PER_TICKER]


def yahoo_news(item, tk):
    found = []
    try:
        raw = tk.news or []
    except Exception:  # noqa: BLE001
        return found
    for n in raw:
        c = n.get("content", n) if isinstance(n, dict) else {}
        title = c.get("title")
        if not title:
            continue
        url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
               or c.get("link") or "")
        source = (c.get("provider") or {}).get("displayName") or c.get("publisher") or "Yahoo Finance"
        pub = to_date(c.get("pubDate") or c.get("displayTime") or c.get("providerPublishTime"))
        if not pub:
            continue
        summary = c.get("summary") or c.get("description") or ""
        found.append({"ticker": item["symbol"], "date": pub, "title": title.strip(), "source": source,
                      "url": url, "summary": cap_words(summary) if summary else None, "via": "yahoo"})
    return found


def article_summary(url):
    """The outlet's own preview text (meta description), never the article body."""
    from bs4 import BeautifulSoup
    real = url
    if "news.google.com" in url:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(url, interval=1)
        if not res or not res.get("status"):
            return "", url
        real = res["decoded_url"]
    r = get(real, timeout=15)
    soup = BeautifulSoup(r.text[:400_000], "html.parser")
    for attr, key in (("property", "og:description"), ("name", "twitter:description"), ("name", "description")):
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content") and len(tag["content"].split()) >= 6:
            return cap_words(tag["content"]), real
    return "", real


def key_of(n):
    return n["ticker"] + "::" + re.sub(r"[^a-z0-9]+", "", n["title"].lower())[:120]


def update_news(store, fresh):
    cutoff = (TODAY - timedelta(days=NEWS_DAYS)).isoformat()
    for n in fresh:
        if n["date"] < cutoff:
            continue
        k = key_of(n)
        old = store.get(k)
        if old is None:
            store[k] = n
            continue
        if not old.get("summary") and n.get("summary"):
            old["summary"] = n["summary"]
        if "news.google.com" in old.get("url", "") and n.get("url") and "news.google.com" not in n["url"]:
            old["url"] = n["url"]          # prefer the outlet's own link
    borrow_summaries(store)
    for k in [k for k, v in store.items() if v["date"] < cutoff]:
        del store[k]
    return store


def borrow_summaries(store):
    """
    An item with no summary takes one from another item about the same story
    (same stock, very similar headline, within 2 days), and says where from.
    """
    by_t = {}
    for n in store.values():
        by_t.setdefault(n["ticker"], []).append(n)
    for items in by_t.values():
        donors = [m for m in items if m.get("summary") and not m.get("borrowed")]
        for n in items:
            if n.get("summary"):
                continue
            d = datetime.fromisoformat(n["date"]).date()
            best = max(donors, default=None, key=lambda m: similar(m["title"], n["title"]))
            if (best is not None and similar(best["title"], n["title"]) >= 0.6
                    and abs((datetime.fromisoformat(best["date"]).date() - d).days) <= 2):
                n["summary"] = f"{best['summary']} [summary from {best['source']}]"
                n["borrowed"] = True


def fill_summaries(store):
    # only items never tried; earlier failures ("") are not retried
    todo = [n for n in store.values() if n.get("summary") is None]
    todo.sort(key=lambda n: n["date"], reverse=True)
    done = 0
    for n in todo[:SUMMARY_FETCH_LIMIT]:
        try:
            summ, real = article_summary(n["url"])
            n["summary"] = summ          # "" means tried and failed; won't retry
            if real and real != n["url"]:
                n["url"] = real
        except Exception:  # noqa: BLE001
            n["summary"] = ""
        done += 1
        time.sleep(0.5)
    left = len(todo) - done
    if left > 0:
        R.warn(f"News: {left} summaries still to fetch; they'll be picked up on the next run")
    missing = sum(1 for n in store.values() if n.get("summary") == "")
    if missing:
        R.warn(f"News: {missing} items have no summary (paywall or site blocks automated visits)")


def trust_tag(n, same_ticker):
    if any(p in n["source"].lower() for p in PRIMARY_SOURCES):
        return "PRIMARY"
    d = datetime.fromisoformat(n["date"]).date()
    for m in same_ticker:
        if m is n or m["source"].lower() == n["source"].lower():
            continue
        if abs((datetime.fromisoformat(m["date"]).date() - d).days) <= 2 and similar(m["title"], n["title"]) >= 0.5:
            return "CORROBORATED"
    return "SINGLE-SOURCE"


def news_lines(store):
    """Returns (items with a summary, headline-only items), each grouped by ticker, newest first."""
    by_t = {}
    for n in store.values():
        by_t.setdefault(n["ticker"], []).append(n)
    with_sum, headline_only = [], []
    for t in sorted(by_t):
        items = sorted(by_t[t], key=lambda n: n["date"], reverse=True)
        for n in items:
            n["tag"] = trust_tag(n, items)
            if n.get("summary"):
                with_sum.append(line(t, n["date"], n["title"], n["source"], n["url"], n["tag"], n["summary"]))
            else:
                headline_only.append(line(t, n["date"], n["title"], n["source"], n["url"], n["tag"], NO_SUMMARY))
    return with_sum, headline_only


# ----------------------------------------------------------------------------
# Writing the files
# ----------------------------------------------------------------------------
def write_text(name, text):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(text.rstrip() + "\n", encoding="utf-8")


def file_index(symbols):
    base = pages_base()
    lines = ["PRICE FILES (one year of daily bars, columns Date, Open, High, Low, Close, Volume):"]
    lines += [f"  {s}: {base}prices/{safe_name(s)}.csv" for s in symbols]
    lines.append("DIVIDEND FILES (columns Date, Type, Amount):")
    lines += [f"  {s}: {base}dividends/{safe_name(s)}.csv" for s in symbols if not s.startswith("^")]
    return "\n".join(lines)


def edit_url():
    """Link that opens watchlist.txt in GitHub's editor."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "main") or "main"
    return f"https://github.com/{repo}/edit/{branch}/watchlist.txt" if "/" in repo else ""


def archive_removed(watch):
    """
    Names taken off the watchlist: move their price and dividend files to
    docs/data/archive/. Names put back on: delete the old archived copy,
    because a fresh full history is downloaded anyway.
    """
    keep = {safe_name(i["symbol"]) for i in watch} | {safe_name(b) for b, _ in BENCHMARKS}
    moved = set()
    for folder in ("prices", "dividends"):
        src, dst = OUT / folder, ARCHIVE / folder
        if src.exists():
            for f in src.glob("*.csv"):
                if f.stem not in keep:
                    dst.mkdir(parents=True, exist_ok=True)
                    f.replace(dst / f.name)
                    moved.add(f.stem)
        if dst.exists():
            for f in dst.glob("*.csv"):
                if f.stem in keep:
                    f.unlink()
    if moved:
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        with open(ARCHIVE / "archive_log.txt", "a", encoding="utf-8") as fh:
            for s in sorted(moved):
                fh.write(f"{TODAY_S} | {s} | removed from watchlist; files moved here\n")
        R.good(f"Archived (no longer on watchlist): {', '.join(sorted(moved))}")
    return sorted(moved)


def archive_news(store, watch):
    """Move news for names no longer on the watchlist out of the live store."""
    live = {i["symbol"] for i in watch}
    gone = {k: v for k, v in store.items() if v["ticker"] not in live}
    if gone:
        path = ARCHIVE / "news_archive.json"
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        old.update(gone)
        path.write_text(json.dumps(old, indent=1, ensure_ascii=False), encoding="utf-8")
        for k in gone:
            del store[k]
    return store


def main():
    watch = read_watchlist()
    print(f"Watchlist: {len(watch)} names")
    archive_removed(watch)
    stock_blocks, dash_stocks, dash_bench, fresh_news = [], [], [], []

    # Stocks
    for item in watch:
        sym = item["symbol"]
        print(f"--- {sym}")
        tk = yf_ticker(sym)
        try:
            hist = fetch_history(tk, sym)
            prices = write_prices(sym, hist)
            if prices.empty:
                raise RuntimeError("no bars in the last year")
        except Exception as e:  # noqa: BLE001
            R.crit(f"{sym}: prices failed - {e}")
            stock_blocks.append(f"## {sym} - {item['name']}\n" + line(f"{sym} latest close", NR, "Price download failed on this run", YAHOO_SRC, yurl(sym, "history/"), TODAY_S))
            time.sleep(2)
            continue
        age = (TODAY - datetime.fromisoformat(prices["Date"].iloc[-1]).date()).days
        if age > STALE_DAYS:
            R.crit(f"{sym}: latest bar is {prices['Date'].iloc[-1]}, {age} days old")
        try:
            divs = write_dividends(sym, tk)
        except Exception as e:  # noqa: BLE001
            divs = pd.DataFrame(columns=["Date", "Type", "Amount"])
            R.warn(f"{sym}: dividend history unavailable ({e.__class__.__name__})")
        try:
            figs, info = stock_figures(item, tk, hist, prices, divs)
        except Exception as e:  # noqa: BLE001
            figs, info = [], {}
            R.warn(f"{sym}: fundamentals failed ({e.__class__.__name__}: {e})")
            traceback.print_exc()
        stock_blocks.append(f"## {sym} - {item['name']} ({item['kind']})\n" + "\n".join(figs))

        try:
            fresh_news += google_news(item)
        except Exception as e:  # noqa: BLE001
            R.warn(f"{sym}: Google News feed failed ({e.__class__.__name__})")
        try:
            fresh_news += bing_news(item)
        except Exception as e:  # noqa: BLE001
            R.warn(f"{sym}: Bing News feed failed ({e.__class__.__name__})")
        fresh_news += yahoo_news(item, tk)

        closes = prices["Close"].tolist()
        dash_stocks.append({
            "symbol": sym, "name": item["name"], "kind": item["kind"], "currency": info.get("currency", ""),
            "sector": info.get("sector") or "", "last_close": closes[-1], "last_date": prices["Date"].iloc[-1],
            "chg_1d": (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 else None,
            "chg_1y": (closes[-1] / closes[0] - 1) * 100 if len(closes) > 1 else None,
            "first_date": prices["Date"].iloc[0], "closes": [round(c, 4) for c in closes],
        })
        R.good(f"{sym}: {len(prices)} bars to {prices['Date'].iloc[-1]}")
        time.sleep(1.5)

    # Benchmarks
    for sym, name in BENCHMARKS:
        try:
            hist = fetch_history(yf_ticker(sym), sym)
            prices = write_prices(sym, hist)
            closes = prices["Close"].tolist()
            dash_bench.append({"symbol": sym, "name": name, "last_close": closes[-1], "last_date": prices["Date"].iloc[-1],
                               "chg_1d": (closes[-1] / closes[-2] - 1) * 100, "chg_1y": (closes[-1] / closes[0] - 1) * 100,
                               "first_date": prices["Date"].iloc[0], "closes": [round(c, 2) for c in closes]})
            R.good(f"{sym}: {len(prices)} bars")
        except Exception as e:  # noqa: BLE001
            R.crit(f"{sym} ({name}): prices failed - {e}")
        time.sleep(1.5)

    # Rates and macro
    macro = market_figures()

    # News
    store_path = OUT / "news_store.json"
    store = json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}
    store = archive_news(store, watch)
    store = update_news(store, fresh_news)
    fill_summaries(store)
    with_sum, headline_only = news_lines(store)
    nlines = with_sum + headline_only
    OUT.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, indent=1, ensure_ascii=False), encoding="utf-8")

    # Files
    status = R.text()
    header = (f"DATA PACK - generated {RUN_STAMP}\n"
              "Format: <figure> | <value> | <definition> | <source> | <URL> | <date>\n"
              f"Anything marked {NR} could not be fetched on this run; do not fill it in by guessing.")
    data_pack = "\n\n".join([header, "# MARKET-WIDE", "\n".join(macro), *stock_blocks])
    news_header = (f"NEWS PACK - last {NEWS_DAYS} days, generated {RUN_STAMP}\n"
                   "Format: <ticker> | <date> | <headline> | <source> | <URL> | <PRIMARY/CORROBORATED/SINGLE-SOURCE> | <facts>\n"
                   "Facts = the outlet's own preview text or the news feed's description (max 50 words), not the full article; "
                   "'[summary from X]' means it was taken from outlet X's report of the same story. "
                   "Trust tags are rule-based: PRIMARY = press wire or official filing; "
                   "CORROBORATED = similar headline from another outlet within 2 days.")
    news_pack = (news_header
                 + f"\n\n# ITEMS WITH A SUMMARY ({len(with_sum)})\n"
                 + ("\n".join(with_sum) if with_sum else "None on this run.")
                 + f"\n\n# HEADLINES ONLY ({len(headline_only)}) - no summary could be retrieved. "
                   "Treat each as unverified: a headline alone can mislead. Open the link before relying on it.\n"
                 + ("\n".join(headline_only) if headline_only else "None."))
    symbols = [i["symbol"] for i in watch] + [b[0] for b in BENCHMARKS]

    write_text("status.txt", status)
    write_text("data_pack.txt", status + "\n\n" + data_pack)
    write_text("news_pack.txt", news_pack)
    write_text("ai_bundle.txt", "\n\n".join([status, file_index(symbols), data_pack, news_pack]))

    recent = sorted(store.values(), key=lambda n: n["date"], reverse=True)
    for s in dash_stocks:
        s["news"] = [{k: n.get(k) for k in ("date", "title", "source", "url", "tag", "summary")}
                     for n in recent if n["ticker"] == s["symbol"]][:6]
    dash = {"generated": RUN_STAMP, "status": {"critical": R.critical, "warnings": R.warnings},
            "stocks": dash_stocks, "benchmarks": dash_bench,
            "macro": [m.split(" | ") for m in macro], "news_count": len(nlines),
            "edit_url": edit_url(), "watchlist_size": len(watch),
            "files": {"bundle": "data/ai_bundle.txt", "data_pack": "data/data_pack.txt",
                      "news_pack": "data/news_pack.txt", "status": "data/status.txt"}}
    write_text("dashboard.json", json.dumps(dash, ensure_ascii=False))

    summary = market_summary(dash_stocks, dash_bench, macro, stock_blocks)
    write_text("summary.md", summary)
    save_history({"status.txt": status, "summary.md": summary,
                  "data_pack.txt": status + "\n\n" + data_pack, "news_pack.txt": news_pack})

    print("\n" + status)
    return 1 if R.critical else 0


# ----------------------------------------------------------------------------
# Daily market summary and dated history
# ----------------------------------------------------------------------------
HISTORY = OUT / "history"
UPCOMING_DAYS = 14


def pct_txt(x):
    return "n/a" if x is None else f"{x:+.1f}%"


def market_summary(stocks, bench, macro, stock_blocks):
    """Short plain-text summary used in the daily email and the history page."""
    out = []

    rates = {}
    for m in macro:
        f = m.split(" | ")
        if f[0] in ("US 1-year Treasury yield", "Singapore 1-year SGS T-bill yield"):
            rates[f[0]] = (f[1], f[5] if len(f) > 5 else "")
    if rates:
        out.append("**Rates:** " + " | ".join(
            f"{'US' if k.startswith('US') else 'SG'} 1-year {v} ({d})" for k, (v, d) in rates.items()))
        out.append("")

    short = []   # names with less than a year of prices

    def table(rows, title, ccy):
        t = [f"**{title}**", "", "| | Last close | Date | 1 day | 1 year |", "|---|---:|---|---:|---:|"]
        for r in rows:
            close = f"{r['last_close']:,.2f}" + (f" {r.get('currency')}" if ccy and r.get("currency") else "")
            yr = pct_txt(r.get("chg_1y"))
            if r.get("first_date") and r["first_date"] > (TODAY - timedelta(days=360)).isoformat():
                yr += " *"
                short.append(f"{r['name']} since {r['first_date']}")
            t.append(f"| {r['name']} | {close} | {r['last_date']} | {pct_txt(r.get('chg_1d'))} | {yr} |")
        return t + [""]

    if bench:
        out += table(bench, "Indices", False)
    if stocks:
        out += table(stocks, "Watchlist", True)
        if short:
            out.append("\\* Less than a year of prices: " + "; ".join(short) + ".")
            out.append("")
        movers = sorted((s for s in stocks if s.get("chg_1d") is not None),
                        key=lambda s: abs(s["chg_1d"]), reverse=True)[:3]
        if movers:
            out.append("**Biggest 1-day moves:** " + ", ".join(
                f"{s['name']} {pct_txt(s['chg_1d'])}" for s in movers))
            out.append("")

    # Results and ex-dividend dates coming up
    horizon = (TODAY + timedelta(days=UPCOMING_DAYS)).isoformat()
    events = []
    for block in stock_blocks:
        for ln in block.splitlines():
            f = ln.split(" | ")
            if len(f) < 2 or not re.match(r"^\d{4}-\d{2}-\d{2}$", f[1]):
                continue
            sym, _, fig = f[0].partition(" ")
            if fig in ("next results date", "ex-dividend date") and TODAY_S <= f[1] <= horizon:
                label = "results" if fig == "next results date" else "ex-dividend"
                events.append((f[1], f"{sym} {label}"))
    if events:
        out.append(f"**Coming up (next {UPCOMING_DAYS} days):** " +
                   "; ".join(f"{d} {e}" for d, e in sorted(events)))
    else:
        out.append(f"**Coming up (next {UPCOMING_DAYS} days):** no results or ex-dividend dates reported.")
    out.append("")
    out.append("Prices are each market's latest close. SGX and US dates can differ because of time zones and holidays.")
    return "\n".join(out)


def save_history(files):
    """Keep a dated copy of today's key files and update the list of dates."""
    day = HISTORY / TODAY_S
    day.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (day / name).write_text(text.rstrip() + "\n", encoding="utf-8")
    dates = sorted((p.name for p in HISTORY.iterdir()
                    if p.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", p.name)), reverse=True)
    (HISTORY / "index.json").write_text(json.dumps({"dates": dates, "updated": RUN_STAMP}), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
