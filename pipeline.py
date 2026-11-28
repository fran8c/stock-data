"""
Daily stock data pipeline.

Runs once a day on GitHub Actions (see .github/workflows/daily.yml) and writes
everything into docs/data/, which GitHub Pages publishes:

  prices/<SYMBOL>.csv     one year of daily bars (real Close, not adjusted)
  dividends/<SYMBOL>.csv  dividends and splits, last two years
                          (also written for peers listed in peers.txt)
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
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import pandas as pd
import requests

import indicators   # the official indicator ledger (indicators.py, same folder)
import newsfilter   # the short, price-relevant news pack (newsfilter.py)
import pillars      # pillar readings as neutral statistics (pillars.py)
import newscontext  # sector, country/macro and global news searches for the copy files (newscontext.py)
import copyfiles    # per-stock copy files for the Individual Stock Harness (copyfiles.py)

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
WATCHLIST = ROOT / "watchlist.txt"
PEERS_FILE = ROOT / "peers.txt"   # peer lists for the Individual Stock Harness
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

# Each market has its own run (see .github/workflows/daily.yml), about 5 hours
# after its close. A run collects only its own market's stocks, benchmarks and
# peers; the other market's figures are kept from its last run.
MARKETS = {
    "SGX": {"tz": SGT, "close": (17, 0)},
    "US": {"tz": ZoneInfo("America/New_York"), "close": (16, 0)},
}
READY_AFTER_HOURS = 2      # a session's bar is expected this long after the close
RETRIES = int(os.environ.get("RETRIES", "2"))                 # re-pulls of files that are behind
RETRY_WAIT_MIN = float(os.environ.get("RETRY_WAIT_MIN", "15"))  # minutes between re-pulls
HOLIDAYS_FILE = ROOT / "holidays.txt"
REPORT_LIST = ROOT / "report_list.txt"   # stocks that get a daily harness copy file
HARNESS_DIR = OUT / "harness"             # the copy files
PARTS = OUT / "parts"      # each market's latest results, combined into the packs

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
    """
    Every message is tagged with a scope: "SGX", "US" (that market's names) or
    "general" (rates, macro, news housekeeping). The status file combines the
    latest messages for each market with this run's general messages.
    """
    def __init__(self):
        self.critical = []   # this run: missing prices or rates -> run fails -> email
        self.warnings = []   # this run: expected gaps -> listed
        self.ok = []
        self.scope = "general"
        self.log = []        # (scope, level, message) for this run

    def _add(self, level, msg):
        self.log.append((self.scope, level, msg))

    def crit(self, msg):
        print("CRITICAL:", msg)
        self.critical.append(msg)
        self._add("crit", msg)

    def warn(self, msg):
        print("WARNING:", msg)
        self.warnings.append(msg)
        self._add("warn", msg)

    def good(self, msg):
        print("OK:", msg)
        self.ok.append(msg)

    def of(self, scope, level):
        return [m for s, lv, m in self.log if s == scope and lv == level]


def status_text(parts, general):
    """
    Combined status: each market's messages from its own latest run, plus the
    general messages from this run. parts = {"SGX": part or None, "US": ...}.
    """
    crit, gaps, runs = [], [], []
    for m in ("SGX", "US"):
        p = parts.get(m)
        if not p:
            runs.append(f"  - {m}: no run yet under the per-market schedule - start the workflow with market = both")
            gaps.append(f"[{m}] no data yet from a {m} run")
            continue
        runs.append(f"  - {m}: {p['run_stamp']}, latest {m} session expected {p.get('expected') or 'n/a'}")
        crit += [f"[{m}] {x}" for x in p["critical"]]
        gaps += [f"[{m}] {x}" for x in p["warnings"]]
    crit += general["critical"]
    gaps += general["warnings"]
    state = "FAILED - see CRITICAL below" if crit else ("OK with gaps" if gaps else "OK")
    lines = [f"RUN STATUS: {state}", f"Run time: {RUN_STAMP}",
             "Market data from (each market runs about 5 hours after its close):", *runs, ""]
    lines.append(f"CRITICAL ({len(crit)}): these gaps break the harnesses")
    lines += [f"  - {x}" for x in crit] or ["  - none"]
    lines.append("")
    lines.append(f"GAPS ({len(gaps)}): expected, marked {NR} in the files")
    lines += [f"  - {x}" for x in gaps] or ["  - none"]
    return "\n".join(lines), crit, gaps


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


def read_peers(path=PEERS_FILE):
    """peers.txt lines: SYMBOL | PEER1, PEER2 (Yahoo symbols). Returns {symbol: [peers]}."""
    peers = {}
    if not path.exists():
        return peers
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "|" not in s:
            continue
        sym, rest = [p.strip() for p in s.split("|", 1)]
        lst = [p.strip().upper() for p in rest.replace(";", ",").split(",") if p.strip()]
        if sym:
            peers[sym.upper()] = lst
    return peers


def market_of(symbol):
    """SGX for .SI names and the STI; US for everything else."""
    return "SGX" if symbol.endswith(".SI") or symbol == "^STI" else "US"


def read_holidays(path=HOLIDAYS_FILE):
    """holidays.txt lines: MARKET | YYYY-MM-DD | name. Returns {"SGX": {dates}, "US": {dates}}."""
    out = {m: set() for m in MARKETS}
    if not path.exists():
        R.warn("holidays.txt not found - market holidays may be reported as stale data")
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) >= 4 and "EARLY" in parts[3].upper():
            continue   # half day: the market still trades, so there is a bar
        if len(parts) >= 2 and parts[0].upper() in out and re.match(r"^\d{4}-\d{2}-\d{2}$", parts[1]):
            out[parts[0].upper()].add(parts[1])
    return out


def is_session(market, d, holidays):
    return d.weekday() < 5 and d.isoformat() not in holidays.get(market, set())


def expected_session(market, holidays, now=None):
    """
    The latest session whose bar should be on Yahoo by now: today (exchange
    time) if it is a trading day and the close was READY_AFTER_HOURS ago,
    otherwise the trading day before. Holidays come from holidays.txt.
    """
    cfg = MARKETS[market]
    local = (now or datetime.now(timezone.utc)).astimezone(cfg["tz"])
    h, m = cfg["close"]
    ready = local.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(hours=READY_AFTER_HOURS)
    d = local.date() if local >= ready else local.date() - timedelta(days=1)
    for _ in range(15):
        if is_session(market, d, holidays):
            return d.isoformat()
        d -= timedelta(days=1)
    return None


def session_open_today(market, now=None):
    """True if the exchange's session for today has not closed yet (bar would be partial)."""
    cfg = MARKETS[market]
    local = (now or datetime.now(timezone.utc)).astimezone(cfg["tz"])
    h, m = cfg["close"]
    return local < local.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(minutes=15), local.date().isoformat()


def behind(last_dates, expected):
    """
    Files that miss their market's latest session: behind the expected session
    date, or behind the newest bar any file in the same market has.
    Returns {symbol: (last bar, should have)}.
    """
    newest = {}
    for sym, d in last_dates.items():
        m = market_of(sym)
        newest[m] = max(newest.get(m, d), d)
    out = {}
    for sym, d in sorted(last_dates.items()):
        m = market_of(sym)
        want = max(x for x in (expected.get(m), newest[m]) if x)
        if d < want:
            out[sym] = (d, want)
    return out


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
            # prepost=False: daily bars are the regular session only (US 09:30-16:00 ET).
            df = tk.history(period="5y", interval="1d", auto_adjust=False, actions=False, prepost=False)
            if df is not None and not df.empty and "Close" in df.columns:
                # A run started by hand while the market is open would get today's
                # unfinished bar; leave it out so every saved bar is a full session.
                still_open, local_today = session_open_today(market_of(symbol))
                if still_open and df.index[-1].strftime("%Y-%m-%d") == local_today:
                    df = df.iloc[:-1]
                    R.good(f"{symbol}: today's unfinished bar left out (market still open)")
                # US 23x5 (from 6 Dec 2026): trades from 21:00 ET to midnight carry the
                # next calendar day's trade date, so Yahoo may show a partial row dated
                # after today (New York). Leave out any row dated after today.
                while len(df) and df.index[-1].strftime("%Y-%m-%d") > local_today:
                    R.good(f"{symbol}: unfinished bar {df.index[-1].strftime('%Y-%m-%d')} left out "
                           "(extended-hours trading only)")
                    df = df.iloc[:-1]
                return df
            last = "empty result"
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"no price data from Yahoo after {tries} tries ({last})")


def us_early_closes(path=HOLIDAYS_FILE):
    """US half days from holidays.txt (lines marked EARLY CLOSE); the regular session ends 13:00 ET."""
    out = set()
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in raw.split("|")]
        if (len(parts) >= 4 and parts[0].upper() == "US" and "EARLY" in parts[3].upper()
                and re.match(r"^\d{4}-\d{2}-\d{2}$", parts[1])):
            out.add(parts[1])
    return out


def us_close_hour(day_iso, early=None):
    """16 on a normal day, 13 on a US half day."""
    return 13 if day_iso in (us_early_closes() if early is None else early) else 16


def us_session_label(ts_et, close_hour=16):
    """US 23x5 session for a New York timestamp (close_hour 13 on a half day)."""
    hm = ts_et.hour * 60 + ts_et.minute
    if close_hour * 60 <= hm < 20 * 60:
        return "AFTER-HOURS"
    if 20 * 60 <= hm < 21 * 60:
        return "PAUSE"
    if hm >= 21 * 60 or hm < 4 * 60:
        return "OVERNIGHT"
    if 4 * 60 <= hm < 9 * 60 + 30:
        return "PRE-MARKET"
    return "REGULAR"


def overnight_snapshot(tk, symbol, last_bar, last_close):
    """
    Latest extended-hours trade after the last bar's regular close (16:00 ET, or
    13:00 ET on a half day listed in holidays.txt), for the
    harness's EXTENDED-HOURS CHECK. Context only: never a bar, a Close or an
    indicator input. Returns a dict, or None if Yahoo has no trade after the close.
    """
    ny = MARKETS["US"]["tz"]
    df = tk.history(period="5d", interval="5m", prepost=True, auto_adjust=False, actions=False)
    if df is None or df.empty or "Close" not in df.columns:
        return None
    idx = df.index.tz_convert(ny) if df.index.tz is not None else df.index.tz_localize(ny)
    df = df.set_axis(idx)
    ch = us_close_hour(last_bar)   # 13:00 ET on a half day, else 16:00
    close_ts = datetime.fromisoformat(last_bar).replace(hour=ch, minute=0, tzinfo=ny)
    after = df[(df.index >= close_ts) & df["Close"].notna()]
    if after.empty:
        return None
    last = after.iloc[-1]
    ts = after.index[-1]
    price = float(last["Close"])
    vol = after["Volume"].fillna(0).sum() if "Volume" in after.columns else None
    if not vol:   # Yahoo often reports 0 volume on extended-hours bars: treat as not available
        vol = None
    return {"price": round(price, 4), "time_et": ts.strftime("%Y-%m-%d %H:%M"),
            "session": us_session_label(ts, ch), "volume_since_close": int(vol) if vol is not None else None,
            "close_time_et": f"{ch}:00",
            "last_close": float(last_close), "last_bar": last_bar,
            "move_pct": round((price / float(last_close) - 1) * 100, 4),
            "source": "Yahoo Finance 5-minute bars with extended hours (prepost=True), via the pipeline"}


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


def keep_dropped_bars(symbol, hist):
    """
    Yahoo sometimes leaves a day out of a later download (on 24 Nov 2026 every
    SGX stock lost its 23 Nov bar). Put back any bar saved on an earlier run
    that Yahoo no longer sends. Yahoo's figures still win for every date it
    does send, so later corrections (e.g. volume) are still picked up.
    """
    path = PRICES / f"{safe_name(symbol)}.csv"
    if hist is None or hist.empty or not path.exists():
        return hist
    saved = pd.read_csv(path, dtype={"Date": str})
    have = set(hist.index.strftime("%Y-%m-%d"))
    newest = hist.index[-1].strftime("%Y-%m-%d")
    lost = saved[~saved["Date"].isin(have) & (saved["Date"] < newest)]
    if lost.empty:
        return hist
    idx = pd.DatetimeIndex(pd.to_datetime(lost["Date"]))
    if hist.index.tz is not None:
        idx = idx.tz_localize(hist.index.tz)
    idx.name = hist.index.name
    add = pd.DataFrame(index=idx, columns=hist.columns, dtype="float64")
    for c in ("Open", "High", "Low", "Close", "Volume"):
        if c in add.columns:
            add[c] = lost[c].astype("float64").values
    if "Adj Close" in add.columns:      # not saved in the files; Close is the closest stand-in
        add["Adj Close"] = add["Close"]
    out = pd.concat([hist, add]).sort_index()
    R.warn(f"{symbol}: Yahoo left out {', '.join(lost['Date'])} - kept the bar(s) saved on an earlier run")
    return out


def missing_sessions(prices, ref_prices, lookback=60):
    """Trading days in the benchmark's last `lookback` bars that this price file lacks."""
    first = prices["Date"].iloc[0]
    have = set(prices["Date"])
    return [d for d in ref_prices["Date"].tail(lookback) if d >= first and d not in have]


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
        try:   # the last release, for the harness's results-date warning
            edf = tk.get_earnings_dates(limit=12)
            past = sorted(d.date().isoformat() for d in edf.index if d.date().isoformat() < TODAY_S)
            if past:
                add("most recent results date", past[-1], "Last earnings release per Yahoo's earnings calendar",
                    yurl(sym), retrieved)
        except Exception:  # noqa: BLE001
            pass
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


def google_news(item, days=NEWS_DAYS):
    sg = item["symbol"].endswith(".SI")
    hl, gl, ceid = ("en-SG", "SG", "SG:en") if sg else ("en-US", "US", "US:en")
    q = f"{item['query']} when:{days}d"
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


def context_search(query, market, days):
    """One sector / country / global search for the copy files: Google News RSS, then Bing News RSS if Google fails or is empty."""
    probe = {"symbol": "CTX.SI" if market == "SGX" else "CTX", "query": query, "name": query}
    try:
        found = google_news(probe, days)
    except Exception:  # noqa: BLE001
        found = []
    if not found:
        found = bing_feed(probe, query)
    return [{k: n.get(k) for k in ("date", "title", "source", "url", "summary")} for n in found]


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


def bars_frame(hist):
    """Yahoo history -> Date, Open, High, Low, Close, Volume with the same rounding as the price files."""
    df = pd.DataFrame({
        "Date": hist.index.strftime("%Y-%m-%d"),
        "Open": hist["Open"].values, "High": hist["High"].values, "Low": hist["Low"].values,
        "Close": hist["Close"].values, "Volume": hist["Volume"].values,
    }).dropna(subset=["Close"])
    for c in ("Open", "High", "Low", "Close"):
        df[c] = df[c].round(4)
    df["Volume"] = df["Volume"].fillna(0).astype("int64")
    return df


def retire_setups():
    """GitHub provides statistics only (21 Nov 2026): remove the old trade-setup files and fields."""
    for f in (OUT / "indicators").glob("*_setups.csv") if (OUT / "indicators").exists() else []:
        f.unlink()
    for f in list((OUT / "indicators").glob("*_latest.json")) + list(HISTORY.glob("*/indicators/*_latest.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if "setups" in d:
                d.pop("setups")
                f.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def archive_indicators(watch):
    """Indicator files of names taken off the watchlist go to archive/indicators/."""
    folder = OUT / "indicators"
    if not folder.exists():
        return
    live = {safe_name(i["symbol"]) for i in watch}
    for f in folder.iterdir():
        if f.name == "COLUMNS.md":
            continue
        stem = re.sub(r"(_weeks|_latest|_ledger|_scores)$", "", f.stem)
        if stem not in live:
            dst = ARCHIVE / "indicators"
            dst.mkdir(parents=True, exist_ok=True)
            f.replace(dst / f.name)


def file_index(symbols, watch_syms=()):
    base = pages_base()
    lines = ["PRICE FILES (one year of daily bars, columns Date, Open, High, Low, Close, Volume):"]
    lines += [f"  {s}: {base}prices/{safe_name(s)}.csv" for s in symbols]
    lines.append("DIVIDEND FILES (columns Date, Type, Amount):")
    lines += [f"  {s}: {base}dividends/{safe_name(s)}.csv" for s in symbols if not s.startswith("^")]
    if watch_syms:
        lines.append("INDICATOR LEDGERS (official pre-computed harness indicators; latest day as text, every day as CSV; "
                     f"column guide: {base}indicators/COLUMNS.md):")
        lines += [f"  {s}: {base}indicators/{safe_name(s)}_ledger.txt | daily {base}indicators/{safe_name(s)}.csv | "
                  f"weekly {base}indicators/{safe_name(s)}_weeks.csv | pillar readings {base}indicators/{safe_name(s)}_scores.csv"
                  for s in watch_syms]
    return "\n".join(lines)


def edit_url():
    """Link that opens watchlist.txt in GitHub's editor."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "main") or "main"
    return f"https://github.com/{repo}/edit/{branch}/watchlist.txt" if "/" in repo else ""


def archive_removed(watch, extra_keep=()):
    """
    Names taken off the watchlist: move their price and dividend files to
    docs/data/archive/. Names put back on: delete the old archived copy,
    because a fresh full history is downloaded anyway.
    """
    keep = ({safe_name(i["symbol"]) for i in watch} | {safe_name(b) for b, _ in BENCHMARKS}
            | {safe_name(s) for s in extra_keep})
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


def selected_markets():
    """--market SGX|US|both on the command line, or the MARKET setting; default both."""
    arg = ""
    if "--market" in sys.argv and sys.argv.index("--market") + 1 < len(sys.argv):
        arg = sys.argv[sys.argv.index("--market") + 1]
    arg = (arg or os.environ.get("MARKET", "") or "both").strip().upper()
    if arg in ("BOTH", "ALL"):
        return ["SGX", "US"]
    if arg in MARKETS:
        return [arg]
    raise SystemExit(f"Unknown market '{arg}' - use SGX, US or both")


def read_part(name):
    p = PARTS / f"{name}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def write_part(name, data):
    PARTS.mkdir(parents=True, exist_ok=True)
    (PARTS / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    markets = selected_markets()
    print(f"Markets on this run: {', '.join(markets)}")
    R.scope = "general"
    holidays = read_holidays()
    watch = read_watchlist()
    print(f"Watchlist: {len(watch)} names")
    peers = read_peers()
    on_list = {i["symbol"] for i in watch} | {b for b, _ in BENCHMARKS}
    peer_only = sorted({p for lst in peers.values() for p in lst} - on_list)
    archive_removed(watch, peer_only)

    run_watch = [i for i in watch if market_of(i["symbol"]) in markets]
    run_bench = [(b, n) for b, n in BENCHMARKS if market_of(b) in markets]
    run_peers = [p for p in peer_only if market_of(p) in markets]
    bench_syms = {b for b, _ in BENCHMARKS}
    expected = {m: expected_session(m, holidays) for m in markets}
    for m in markets:
        print(f"{m}: latest session expected in the data = {expected[m]}")

    # 1. Prices. Every pull rewrites the whole file, so Yahoo's later revisions
    #    of recent bars (e.g. volume) are picked up each run.
    tks, hist, prices, failed = {}, {}, {}, {}

    def pull(sym, fresh=False):
        R.scope = market_of(sym)
        if fresh or sym not in tks:
            tks[sym] = yf_ticker(sym)
        try:
            h = keep_dropped_bars(sym, fetch_history(tks[sym], sym))
            p = write_prices(sym, h)
            if p.empty:
                raise RuntimeError("no bars in the last year")
            hist[sym], prices[sym] = h, p
            failed.pop(sym, None)
        except Exception as e:  # noqa: BLE001
            failed[sym] = e
        time.sleep(1.5)

    for sym in [i["symbol"] for i in run_watch] + [b for b, _ in run_bench] + run_peers:
        print(f"--- {sym} prices")
        pull(sym)

    def last_dates():
        return {s: p["Date"].iloc[-1] for s, p in prices.items()}

    # Re-pull any file that misses its market's latest session.
    lag = behind(last_dates(), expected)
    for attempt in range(1, RETRIES + 1):
        if not lag:
            break
        print(f"Behind: {', '.join(lag)} - waiting {RETRY_WAIT_MIN:g} min, then re-pull {attempt} of {RETRIES}")
        time.sleep(RETRY_WAIT_MIN * 60)
        for sym in list(lag):
            pull(sym, fresh=True)
        lag = behind(last_dates(), expected)

    stale = {m: [] for m in markets}
    for sym, (d, want) in lag.items():
        m = market_of(sym)
        R.scope = m
        kind = "benchmark" if sym in bench_syms else ("peer price file" if sym in run_peers else "price file")
        R.warn(f"{sym}: {kind} ends {d}, misses the {m} session of {want} after {RETRIES} re-pull(s) - "
               "the harness will mark that session as not in the data")
        stale[m].append({"symbol": sym, "last": d, "expected": want})
    for m in markets:
        if not stale[m]:
            R.scope = m
            R.good(f"Lag check: every {m} price file has the {expected[m]} session")

    # Gap check: a day missing in the middle of a file (the lag check above only
    # looks at the last bar). Compared with the market's benchmark index.
    ref_sym = {"SGX": "^STI", "US": "^GSPC"}
    for sym, p in sorted(prices.items()):
        m = market_of(sym)
        ref = ref_sym.get(m)
        if not ref or sym in bench_syms:
            continue
        ref_p = prices.get(ref)
        if ref_p is None and (PRICES / f"{safe_name(ref)}.csv").exists():
            ref_p = pd.read_csv(PRICES / f"{safe_name(ref)}.csv", dtype={"Date": str})
        if ref_p is None:
            continue
        gaps = missing_sessions(p, ref_p)
        R.scope = m
        if gaps:
            R.crit(f"{sym}: price file is missing {', '.join(gaps)} (the {ref} file has these days) - "
                   "figures spanning these days are computed without them")
    for b in [b for b, _ in run_bench]:
        if b in prices and int(prices[b]["Volume"].iloc[-1]) == 0 and len(prices[b]) > 1 \
                and int(prices[b]["Volume"].iloc[-2]) > 0:
            R.scope = market_of(b)
            R.warn(f"{b}: latest bar {prices[b]['Date'].iloc[-1]} has zero volume - Yahoo may not have finished it")

    # 2. Stocks: figures, indicator ledger, news and dashboard rows
    blocks, dash_stocks, dash_bench, fresh_news = {}, {}, {}, []
    indicator_latest, indicator_frames = {}, {}
    archive_indicators(watch)
    retire_setups()
    for item in run_watch:
        sym = item["symbol"]
        R.scope = market_of(sym)
        print(f"--- {sym}")
        if sym in failed:
            R.crit(f"{sym}: prices failed - {failed[sym]}")
            blocks[sym] = f"## {sym} - {item['name']}\n" + line(f"{sym} latest close", NR, "Price download failed on this run", YAHOO_SRC, yurl(sym, "history/"), TODAY_S)
            continue
        tk, h, p = tks[sym], hist[sym], prices[sym]
        age = (TODAY - datetime.fromisoformat(p["Date"].iloc[-1]).date()).days
        if age > STALE_DAYS:
            R.crit(f"{sym}: latest bar is {p['Date'].iloc[-1]}, {age} days old")
        try:
            divs = write_dividends(sym, tk)
        except Exception as e:  # noqa: BLE001
            divs = pd.DataFrame(columns=["Date", "Type", "Amount"])
            R.warn(f"{sym}: dividend history unavailable ({e.__class__.__name__})")
        try:
            figs, info = stock_figures(item, tk, h, p, divs)
        except Exception as e:  # noqa: BLE001
            figs, info = [], {}
            R.warn(f"{sym}: fundamentals failed ({e.__class__.__name__}: {e})")
            traceback.print_exc()
        blocks[sym] = f"## {sym} - {item['name']} ({item['kind']})\n" + "\n".join(figs)

        # Official indicator ledger: every harness indicator for every day of the
        # last year, rebuilt each run from up to 5 years of bars.
        try:
            m = market_of(sym)
            bench = ["^STI"] if m == "SGX" else ["^GSPC", "^IXIC", "^DJI"]
            comps = {}
            for c_sym in bench + peers.get(sym, []):
                if c_sym in hist:
                    comps[c_sym] = bars_frame(hist[c_sym])
                elif (PRICES / f"{safe_name(c_sym)}.csv").exists():
                    comps[c_sym] = pd.read_csv(PRICES / f"{safe_name(c_sym)}.csv")
            d_ind, w_ind, latest = indicators.build(sym, bars_frame(h), divs, comps, bench[0], m, TODAY_S, expected[m])
            if m == "US":
                # Extended-hours price since the 16:00 ET close (23x5 trading); context only.
                try:
                    lb = latest["last_bar"]
                    lc = float(bars_frame(h)["Close"].iloc[-1])
                    latest["overnight"] = overnight_snapshot(tk, sym, lb, lc)
                    if latest["overnight"]:
                        o = latest["overnight"]
                        R.good(f"{sym}: extended hours {o['move_pct']:+.2f}% at {o['price']} ({o['session']} {o['time_et']} ET)")
                except Exception as e:  # noqa: BLE001
                    latest["overnight"] = None
                    R.warn(f"{sym}: extended-hours snapshot unavailable ({e.__class__.__name__}: {e})")
            indicators.write_outputs(sym, d_ind, w_ind, latest)
            indicator_latest[sym] = latest
            indicator_frames[sym] = (item, d_ind, w_ind, m)
            R.good(f"{sym}: indicator ledger {len(d_ind)} days to {latest['last_bar']}")
        except Exception as e:  # noqa: BLE001
            R.warn(f"{sym}: indicator ledger failed ({e.__class__.__name__}: {e})")
            traceback.print_exc()

        try:
            fresh_news += google_news(item)
        except Exception as e:  # noqa: BLE001
            R.warn(f"{sym}: Google News feed failed ({e.__class__.__name__})")
        try:
            fresh_news += bing_news(item)
        except Exception as e:  # noqa: BLE001
            R.warn(f"{sym}: Bing News feed failed ({e.__class__.__name__})")
        fresh_news += yahoo_news(item, tk)

        closes = p["Close"].tolist()
        dash_stocks[sym] = {
            "symbol": sym, "name": item["name"], "kind": item["kind"], "currency": info.get("currency", ""),
            "sector": info.get("sector") or "", "last_close": closes[-1], "last_date": p["Date"].iloc[-1],
            "chg_1d": (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 else None,
            "chg_1y": (closes[-1] / closes[0] - 1) * 100 if len(closes) > 1 else None,
            "first_date": p["Date"].iloc[0], "closes": [round(c, 4) for c in closes],
        }
        R.good(f"{sym}: {len(p)} bars to {p['Date'].iloc[-1]}")
        time.sleep(1.5)

    # Benchmarks and peers
    for sym, name in run_bench:
        R.scope = market_of(sym)
        if sym in failed:
            R.crit(f"{sym} ({name}): prices failed - {failed[sym]}")
            continue
        closes = prices[sym]["Close"].tolist()
        dash_bench[sym] = {"symbol": sym, "name": name, "last_close": closes[-1], "last_date": prices[sym]["Date"].iloc[-1],
                           "chg_1d": (closes[-1] / closes[-2] - 1) * 100, "chg_1y": (closes[-1] / closes[0] - 1) * 100,
                           "first_date": prices[sym]["Date"].iloc[0], "closes": [round(c, 2) for c in closes]}
        R.good(f"{sym}: {len(prices[sym])} bars")
    for sym in run_peers:
        R.scope = market_of(sym)
        if sym in failed:
            R.warn(f"{sym} (peer): prices {NR} - {failed[sym]}")
            continue
        try:
            write_dividends(sym, tks[sym])
        except Exception:  # noqa: BLE001
            pass
        R.good(f"{sym} (peer): {len(prices[sym])} bars")
    for m in markets:
        R.scope = m
        no_peers = [i["symbol"] for i in run_watch if market_of(i["symbol"]) == m and not peers.get(i["symbol"])]
        if no_peers:
            R.warn("No peers in peers.txt for: " + ", ".join(no_peers) + " - the harness will suggest some")

    # 3. Rates and macro (both markets' rates, every run)
    R.scope = "general"
    macro = market_figures()

    # 4. News: this run's market adds fresh items; the store keeps both markets
    store_path = OUT / "news_store.json"
    store = json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}
    store = archive_news(store, watch)
    store = update_news(store, fresh_news)
    fill_summaries(store)
    with_sum, headline_only = news_lines(store)
    nlines = with_sum + headline_only
    OUT.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, indent=1, ensure_ascii=False), encoding="utf-8")

    # 5. Save this run's market results, then combine with the other market's latest
    for m in markets:
        write_part(m, {
            "market": m, "run_stamp": RUN_STAMP, "expected": expected[m],
            "critical": R.of(m, "crit"), "warnings": R.of(m, "warn"), "stale": stale[m],
            "blocks": {s: b for s, b in blocks.items() if market_of(s) == m},
            "stocks": {s: d for s, d in dash_stocks.items() if market_of(s) == m},
            "bench": {s: d for s, d in dash_bench.items() if market_of(s) == m},
        })
    general = {"run_stamp": RUN_STAMP, "critical": R.of("general", "crit"),
               "warnings": R.of("general", "warn")}
    parts = {m: read_part(m) for m in MARKETS}

    stock_blocks, all_stocks, all_bench = [], [], []
    for item in watch:
        sym = item["symbol"]
        p = parts.get(market_of(sym)) or {}
        stock_blocks.append(p.get("blocks", {}).get(sym) or (
            f"## {sym} - {item['name']}\n" + line(f"{sym} latest close", NR,
            f"No {market_of(sym)} run has collected this name yet", YAHOO_SRC, yurl(sym, "history/"), TODAY_S)))
        if sym in p.get("stocks", {}):
            all_stocks.append(p["stocks"][sym])
    for sym, _ in BENCHMARKS:
        p = parts.get(market_of(sym)) or {}
        if sym in p.get("bench", {}):
            all_bench.append(p["bench"][sym])

    # Files
    status, crit_all, gaps_all = status_text(parts, general)
    header = (f"DATA PACK - generated {RUN_STAMP}\n"
              "Format: <figure> | <value> | <definition> | <source> | <URL> | <date>\n"
              f"Anything marked {NR} could not be fetched on this run; do not fill it in by guessing.\n"
              "SGX and US names are collected by separate runs about 5 hours after each market's close; "
              "the status above shows when each market was last collected.")
    data_pack = "\n\n".join([header, "# MARKET-WIDE", "\n".join(macro), *stock_blocks])
    # Full 30-day list (for reference) and the short, price-relevant pack (for the harness)
    all_header = (f"ALL NEWS - last {NEWS_DAYS} days, unfiltered, generated {RUN_STAMP}. For reference only: "
                  "the harness uses news_pack.txt, which keeps only price-relevant company items.\n"
                  "Format: <ticker> | <date> | <headline> | <source> | <URL> | <PRIMARY/CORROBORATED/SINGLE-SOURCE> | <facts>")
    news_all = (all_header
                + f"\n\n# WITH A SUMMARY ({len(with_sum)})\n" + ("\n".join(with_sum) if with_sum else "None.")
                + f"\n\n# HEADLINE ONLY ({len(headline_only)})\n" + ("\n".join(headline_only) if headline_only else "None."))
    try:
        sections, kept_news, ncount = newsfilter.build_pack(store, watch, OUT / "indicators", TODAY_S, clean, NO_SUMMARY)
    except Exception as e:  # noqa: BLE001
        R.scope = "general"
        R.warn(f"News pack filter failed ({e.__class__.__name__}: {e}); news_pack.txt holds the unfiltered list")
        traceback.print_exc()
        sections, kept_news, ncount = None, {}, None
    # Pillar readings as neutral statistics for this run's stocks (no trade levels or labels)
    for sym, (item, d_ind, w_ind, m) in indicator_frames.items():
        R.scope = m
        try:
            ratings = pillars.rating_items(store, item)
            s_hist, s_latest = pillars.build(sym, d_ind, indicator_latest[sym], m, ratings)
            latest = indicator_latest[sym]
            latest["scores"] = s_latest
            latest["extra_ledger"] = pillars.ledger_lines(s_latest)
            indicators.write_outputs(sym, d_ind, w_ind, latest)
            s_hist.to_csv(OUT / "indicators" / f"{safe_name(sym)}_scores.csv", index=False, float_format="%.4f")
            R.good(f"{sym}: scores - microstructure {s_latest['microstructure']['label']}, momentum {s_latest['momentum']['label']}"
                   if s_latest else f"{sym}: scores not computed")
        except Exception as e:  # noqa: BLE001
            R.warn(f"{sym}: pillar statistics failed ({e.__class__.__name__}: {e})")
            traceback.print_exc()

    if sections is not None:
        news_header = (f"NEWS PACK - price-relevant company news, {newsfilter.WINDOW_DAYS} days to each stock's last bar, "
                       f"at most {newsfilter.MAX_ITEMS} items per stock, generated {RUN_STAMP}\n"
                       "Kept: items about the company itself (name in the headline; not research it publishes about others; not an "
                       "affiliate trust or REIT; not one name in a list) of these event types: RESULTS, GUIDANCE, RATING CHANGE "
                       "(maintained calls and target-only changes are dropped), DEAL, CAPITAL, REGULATORY / LEGAL, MANAGEMENT, INDEX. "
                       "Commentary, opinion, forecasts and CSR are dropped. Rules are keyword-based: check an item before relying on it.\n"
                       "Format: <date> | <event type> | <headline> | <source> | <URL> | <PRIMARY/CORROBORATED/SINGLE-SOURCE> | "
                       "day <trading day>: chg <change %> gap <gap %> vol <volume ratio> [BIG MOVE] | <facts, or the no-summary note>\n"
                       "The day figures come from the indicator ledger (chg is dividend-adjusted). BIG MOVE = |change| >= 1.5%, "
                       "|gap| >= 1.0% or volume >= 1.5x normal. 'BIG-MOVE DAYS WITH NO NEWS KEPT' lists days to search for news.\n"
                       "Sector, country and global news: in the harness copy files (docs/data/harness/) for the stocks on report_list.txt.\n"
                       f"Totals: # ITEMS WITH A SUMMARY ({ncount['with_summary']})  # HEADLINES ONLY ({ncount['headline_only']}) "
                       "(headline-only items are unverified: open the link before relying on them). "
                       "The full 30-day list is in news_all.txt.")
        news_pack = news_header + "\n\n" + "\n\n".join(sections)
    else:
        news_pack = news_all
    symbols = [i["symbol"] for i in watch] + [b[0] for b in BENCHMARKS] + peer_only

    write_text("status.txt", status)
    write_text("data_pack.txt", status + "\n\n" + data_pack)
    write_text("news_pack.txt", news_pack)
    write_text("news_all.txt", news_all)
    peer_lines = ["PEERS (from peers.txt; use as peer_data in the Individual Stock Harness):"]
    peer_lines += [f"  {s}: {', '.join(peers.get(s, [])) or 'none listed - the harness will suggest peers'}" for s in [i["symbol"] for i in watch]]
    write_text("peers.txt", "\n".join(peer_lines))
    write_text("ai_bundle.txt", "\n\n".join([status, file_index(symbols, [i["symbol"] for i in watch]), "\n".join(peer_lines), data_pack, news_pack]))

    # Individual Stock Harness copy files (report_list.txt) for this run's markets
    copy_written = write_copy_files(markets, watch, peers, store, kept_news, blocks)
    copy_index = copy_file_index(watch)

    recent = sorted(store.values(), key=lambda n: n["date"], reverse=True)
    for s in all_stocks:
        # the pillar readings (statistics only), from the indicator files (this run or the market's last run)
        try:
            lt = json.loads((OUT / "indicators" / f"{safe_name(s['symbol'])}_latest.json").read_text(encoding="utf-8"))
            sc = lt.get("scores") or {}
            s["scores"] = {"confluence": (lt.get("values") or {}).get("confluence_final"),
                           "micro": (sc.get("microstructure") or {}).get("label"),
                           "momentum": (sc.get("momentum") or {}).get("label"),
                           "upgrades": (sc.get("ratings_14d") or {}).get("upgrades"),
                           "downgrades": (sc.get("ratings_14d") or {}).get("downgrades")} if sc else None
            on = lt.get("overnight")
            if s["scores"] is not None and on:
                s["scores"]["since_close"] = {k: on.get(k) for k in ("move_pct", "price", "session", "time_et")}
        except Exception:  # noqa: BLE001
            s["scores"] = None
        if s["symbol"] in kept_news:   # the filtered, price-relevant items
            s["news"] = [{**{k: n.get(k) for k in ("date", "title", "source", "url", "tag", "summary")},
                          "event": n.get("event"), "big_move": bool((n.get("rx") or {}).get("big"))}
                         for n in kept_news[s["symbol"]]][:6]
        else:
            s["news"] = [{k: n.get(k) for k in ("date", "title", "source", "url", "tag", "summary")}
                         for n in recent if n["ticker"] == s["symbol"]][:6]
    dash = {"generated": RUN_STAMP, "status": {"critical": crit_all, "warnings": gaps_all},
            "market_runs": {m: (parts[m] or {}).get("run_stamp") for m in MARKETS},
            "stocks": all_stocks, "benchmarks": all_bench,
            "macro": [m.split(" | ") for m in macro], "news_count": len(nlines),
            "edit_url": edit_url(), "watchlist_size": len(watch),
            "files": {"bundle": "data/ai_bundle.txt", "data_pack": "data/data_pack.txt",
                      "news_pack": "data/news_pack.txt", "status": "data/status.txt",
                      "indicators": "data/indicators/"},
            "copy_files": copy_index}
    write_text("dashboard.json", json.dumps(dash, ensure_ascii=False))

    summary = market_summary(all_stocks, all_bench, macro, stock_blocks)
    if copy_index:
        base = pages_base()
        summary += ("\n\n**Harness copy files** (paste one into an AI chat; the data-only file is for an AI with the harness saved)\n"
                    + "\n".join(f"- {c['name']} ({c['ticker']}, as of {c['as_of']}): full {base}{c['full'][5:]} | data only {base}{c['data'][5:]}"
                                 if c["full"] else f"- {c['name']} ({c['ticker']}, as of {c['as_of']}): data only {base}{c['data'][5:]}"
                                 for c in copy_index))
    write_text("summary.md", summary)
    save_history({"status.txt": status, "summary.md": summary,
                  "data_pack.txt": status + "\n\n" + data_pack, "news_pack.txt": news_pack})
    # Keep this run's copy files in the dated folder
    if copy_written:
        hdir = HISTORY / TODAY_S / "harness"
        hdir.mkdir(parents=True, exist_ok=True)
        for s_ in copy_written:
            for suffix in ("", "_data"):
                f = HARNESS_DIR / f"{safe_name(s_)}{suffix}.txt"
                if f.exists():
                    (hdir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        (hdir / "index.json").write_text(json.dumps(sorted(p.name for p in hdir.glob("*.txt"))), encoding="utf-8")
    # Freeze this run's official latest-day indicators in the dated folder
    if indicator_latest:
        frozen = HISTORY / TODAY_S / "indicators"
        frozen.mkdir(parents=True, exist_ok=True)
        for s_, lt in indicator_latest.items():
            (frozen / f"{safe_name(s_)}_latest.json").write_text(
                json.dumps({k: v for k, v in lt.items() if k != "extra_ledger"}, indent=1, default=str), encoding="utf-8")

    # What this run did, for the report notice and the STALE alert (see daily.yml)
    write_text("run_result.json", json.dumps({
        "run_stamp": RUN_STAMP, "markets": markets, "expected": expected,
        "critical": R.critical, "warnings": R.warnings, "stale": stale,
        "retries": RETRIES, "retry_wait_min": RETRY_WAIT_MIN}, ensure_ascii=False, indent=1))

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


def market_as_of(m):
    """The harness reads as_of_date in the exchange's local time (US runs happen on the next SGT day)."""
    return NOW.astimezone(MARKETS[m]["tz"]).date().isoformat()


def write_copy_files(markets, watch, peers, store, kept_news, blocks):
    """
    Copy files for the Individual Stock Harness, for the stocks on report_list.txt in this run's markets:
    docs/data/harness/<SYMBOL>.txt (harness + run inputs) and <SYMBOL>_data.txt (run inputs only).
    Returns {symbol: {"full": chars, "data": chars}} for the files written on this run.
    """
    report = newscontext.read_report_list(REPORT_LIST)
    by_sym = {i["symbol"]: i for i in watch}
    HARNESS_DIR.mkdir(parents=True, exist_ok=True)
    keep = {safe_name(r["symbol"]) for r in report}
    for f in HARNESS_DIR.glob("*.txt"):   # a stock taken off report_list.txt loses its copy files
        if f.stem.removesuffix("_data") not in keep:
            f.unlink()
    try:
        harness_text = (ROOT / copyfiles.HARNESS_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        # No harness in the repo (kept private): data-only files, and no full copies left anywhere on the site
        harness_text = None
        print(f"{copyfiles.HARNESS_FILE} not in the repo: writing data-only copy files")
        for f in [*HARNESS_DIR.glob("*.txt"), *HISTORY.glob("*/harness/*.txt")]:
            if not f.stem.endswith("_data"):
                f.unlink()
        for idx in HISTORY.glob("*/harness/index.json"):
            idx.write_text(json.dumps(sorted(p.name for p in idx.parent.glob("*.txt"))), encoding="utf-8")
    written = {}
    for m in markets:
        R.scope = m
        rep_m = [r for r in report if market_of(r["symbol"]) == m]
        missing = [r["symbol"] for r in rep_m if r["symbol"] not in by_sym]
        if missing:
            R.warn(f"report_list.txt: {', '.join(missing)} not on watchlist.txt - no copy file")
        rep_m = [r for r in rep_m if r["symbol"] in by_sym
                 and (OUT / "indicators" / f"{safe_name(r['symbol'])}_ledger.txt").exists()]
        if not rep_m:
            continue
        as_of = market_as_of(m)
        try:
            ctx = newscontext.run(rep_m, by_sym, str(OUT / "indicators"), m, as_of, context_search)
        except Exception as e:  # noqa: BLE001
            R.warn(f"{m}: sector, country and global news searches failed ({e.__class__.__name__}: {e})")
            traceback.print_exc()
            ctx = {}
        bench = ["^STI"] if m == "SGX" else ["^GSPC", "^IXIC", "^DJI"]
        for r in rep_m:
            sym, item = r["symbol"], by_sym[r["symbol"]]
            try:
                prices_df = pd.read_csv(PRICES / f"{safe_name(sym)}.csv")
                comps = []
                for c in bench + peers.get(sym, []):
                    f = PRICES / f"{safe_name(c)}.csv"
                    if f.exists():
                        name = dict(BENCHMARKS).get(c) or (by_sym.get(c) or {}).get("name") or c
                        comps.append((f"{name}, {c}", "benchmark_data" if c in bench else "peer_data", pd.read_csv(f)))
                dv = OUT / "dividends" / f"{safe_name(sym)}.csv"
                divs = pd.read_csv(dv) if dv.exists() else None
                lb = prices_df["Date"].iloc[-1]
                hol = copyfiles.read_holidays(HOLIDAYS_FILE, m, lb,
                                              (datetime.fromisoformat(lb) + timedelta(days=21)).date().isoformat())
                ledger = (OUT / "indicators" / f"{safe_name(sym)}_ledger.txt").read_text(encoding="utf-8")
                try:
                    on = json.loads((OUT / "indicators" / f"{safe_name(sym)}_latest.json")
                                    .read_text(encoding="utf-8")).get("overnight")
                except Exception:  # noqa: BLE001
                    on = None
                full, data = copyfiles.build(r, item, m, as_of, RUN_STAMP, ledger, prices_df, comps, divs, hol,
                                             copyfiles.pack_figures(blocks.get(sym)), ctx.get(sym), kept_news.get(sym),
                                             store, clean, harness_text or "", bing_queries(item), overnight=on)
                if harness_text:
                    (HARNESS_DIR / f"{safe_name(sym)}.txt").write_text(full, encoding="utf-8")
                (HARNESS_DIR / f"{safe_name(sym)}_data.txt").write_text(data, encoding="utf-8")
                written[sym] = {"full": len(full) if harness_text else None, "data": len(data)}
                R.good(f"{sym}: harness copy file {len(full):,} characters (data only {len(data):,})" if harness_text
                       else f"{sym}: harness data file {len(data):,} characters")
            except Exception as e:  # noqa: BLE001
                R.warn(f"{sym}: harness copy file failed ({e.__class__.__name__}: {e})")
                traceback.print_exc()
    return written


def copy_file_index(watch):
    """Every copy file on disk (this run's market and the other market's last run), for the dashboard and email."""
    names = {i["symbol"]: i["name"] for i in watch}
    out = []
    for r in newscontext.read_report_list(REPORT_LIST):
        stem = safe_name(r["symbol"])
        full, data = HARNESS_DIR / f"{stem}.txt", HARNESS_DIR / f"{stem}_data.txt"
        if not data.exists():
            continue
        head = data.read_text(encoding="utf-8")[:2000]
        as_of = re.search(r"^as of: (\S+)", head, re.M)
        out.append({"symbol": r["symbol"], "name": names.get(r["symbol"], r["symbol"]), "ticker": r["ticker"],
                    "as_of": as_of.group(1) if as_of else None,
                    "full": f"data/harness/{stem}.txt" if full.exists() else None, "data": f"data/harness/{stem}_data.txt",
                    "full_chars": full.stat().st_size if full.exists() else None, "data_chars": data.stat().st_size})
    return out


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
