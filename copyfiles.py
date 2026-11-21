"""
Per-stock copy files for the Individual Stock Harness (decided 21 Nov 2026).

For each stock on report_list.txt, each run of its market writes two files to docs/data/harness/:
  <SYMBOL>.txt       the full harness text, then the run inputs: one paste works in any AI chat
  <SYMBOL>_data.txt  the run inputs only, for an AI that already has the harness saved (a Claude Project,
                     a Gemini Gem)
The run inputs use the harness's plain labels: as of, ticker, settings, last_results_date, raw_market_data
(one year), benchmark_data and peer_data (last COMP_ROWS rows), corporate_actions, holidays, github_ledger
(the official indicator ledger) and news_payload (a news pack in the harness's template format: company items
from the daily feeds plus the sector, country/macro and global searches from newscontext.py).
"""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import newscontext as C

HARNESS_FILE = "harness/Individual Stock Harness v3.3.txt"
HARNESS_NAME = "Individual Stock Harness v3.3"
COMP_ROWS = 90
MINIMUM = {"COMPANY": 5, "SECTOR": 2, "COUNTRY/MACRO": 3, "GLOBAL/REGIONAL": 2}
HEADLINE_ONLY = "headline only (no summary in the feed; open the link before relying on it)"


def pack_figures(block):
    """{figure: (value, definition, source, url, date)} from the stock's data-pack block."""
    out = {}
    for ln in (block or "").splitlines():
        p = [x.strip() for x in ln.split(" | ")]
        if len(p) == 6 and " " in p[0]:
            out[p[0].split(" ", 1)[1]] = tuple(p[1:])
    return out


def csv_text(df, cols):
    return df[cols].to_csv(index=False, lineterminator="\n").strip()


def read_holidays(path, market, start, end):
    out = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        p = [x.strip() for x in ln.split("|")]
        if ln.startswith("#") or len(p) < 3 or p[0] != market:
            continue
        if start < p[1] <= end:
            out.append(f"{p[1]},{'EARLY CLOSE' if len(p) > 3 and 'EARLY' in p[3].upper() else 'CLOSED'},{p[2]}")
    return out


def company_log(store, sym, item, start, end, bing_q):
    """Search-log lines for the daily company feeds, counted from the 30-day news store."""
    rows = [n for n in store.values() if n["ticker"] == sym]
    def cnt(via):
        r = [n for n in rows if n.get("via") == via]
        return len(r), sum(1 for n in r if start <= n["date"] <= end)
    g, b, y = cnt("google"), cnt("bing"), cnt("yahoo")
    bq = "; ".join(bing_q)
    return [f"COMPANY | Google News: {item['query']} (daily, last 30 days kept) | {g[0]} | {g[1]}",
            f"COMPANY | Bing News, 3 queries: {bq} (daily, counts combined) | {b[0]} | {b[1]}",
            f"COMPANY | Yahoo Finance news feed for {sym} (daily) | {y[0]} | {y[1]}"], 5


def news_pack(rep, item, market, as_of, ctx, kept, store, figs, run_stamp, clean, bing_q):
    m = C.MARKETS[market]
    start, end = ctx["window"] if ctx else C.window(as_of, as_of)
    starts = [k["date"] for k in kept or []]
    if starts:   # the company filter's window runs from the last bar minus 14 days: cover both
        start = min(start, min(starts))
    peers = ", ".join(rep["peer_names"]) or "none named"
    L = [f"NEWS PACK - from the user's own GitHub pipeline (fran8c/stock-data), run {run_stamp}. Collected by keyword rules "
         "from news feeds (Google News, Bing News, Yahoo Finance): every item is a candidate to check under news_rules D to I.",
         f"Company: {item['name']} ({rep['ticker']}; {m['country']}) | sector words searched: {rep['sector']} | peers: {peers} | "
         f"home index: {m['index_name']} | region: {m['region'] if market == 'SGX' else 'US/global'}",
         f"Window: {start} to {end}", "SEARCH LOG"]
    clog, n_company = company_log(store, item["symbol"], item, start, end, bing_q)
    L += clog
    done = {"COMPANY": n_company}
    failed = set()
    for lg in (ctx or {}).get("log", []):
        status = "" if lg["status"] == "ok" else f" [{lg['status']}]"
        L.append(f"{lg['scope']} | {lg['code']}: {lg['query']}{status} | {lg['reviewed']} | {lg['in_window']}")
        if lg["status"] == "ok":
            done[lg["scope"]] = done.get(lg["scope"], 0) + 1
        else:
            failed.add(lg["scope"])
    if not ctx:
        L.append("SECTOR, COUNTRY/MACRO, GLOBAL/REGIONAL | not searched on this run | 0 | 0")
    L.append("ITEMS")
    rows = []
    for k in kept or []:
        rx = k.get("rx") or {}
        day = (f"reaction day {rx['day']}: chg {rx['chg']:+.2f}%, gap {rx['gap']:+.2f}%, vol {rx['vr']:.2f}x"
               + (" BIG MOVE" if rx.get("big") else "")) if rx.get("day") else "reaction day not in the data"
        facts = f"{k.get('event')}; {day}; {k.get('summary') or HEADLINE_ONLY}"
        rows.append(("COMPANY", k["date"], k["title"], k["source"], k["url"], k.get("tag") or "SINGLE-SOURCE", facts))
    seen = [(k["url"], k["title"]) for k in kept or []]
    for k in (ctx or {}).get("items", []):
        if any(k["url"] == u or C.N.similar(k["title"], t) >= 0.6 for u, t in seen):
            continue   # already in the company items
        via = f"found by {k['via']}; " if k["via"].startswith("D ") else ""
        rows.append((k["scope"], k["date"], k["title"], k["source"], k["url"], k["tag"], via + (k.get("summary") or HEADLINE_ONLY)))
    order = {"COMPANY": 0, "SECTOR": 1, "COUNTRY/MACRO": 2, "GLOBAL/REGIONAL": 3}
    for r in sorted(rows, key=lambda r: (order.get(r[0], 9), r[1]), reverse=False):
        L.append(" | ".join(clean(str(x)) for x in (r[0], r[1], r[2], r[3], r[4], r[5], "NEW/RECYCLED not checked", r[6], "none stated")))
    if not rows:
        L.append("none")
    L += ["BACKGROUND (up to 2: dated up to 30 days before the window and still in force)",
          "none collected (the pipeline does not search for background items)", "CATALYSTS (future-dated events)"]
    cats = []
    for fig, ev in (("next results date", "results release (Yahoo date; estimated if the company has not confirmed)"),
                    ("ex-dividend date", "ex-dividend date")):
        v = figs.get(fig)
        if v and v[0][:4].isdigit() and v[0] >= as_of:
            cats.append(f"{v[0]} | {ev} | {v[2]} | {v[3]} | {v[4]}")
    L += cats or ["none found in the pipeline's data (run the catalyst query C5 if you can search)"]
    L.append("NOT FOUND")
    nf = []
    by_scope = {}
    for r in rows:
        by_scope[r[0]] = by_scope.get(r[0], 0) + 1
    for sc, mn in MINIMUM.items():
        bits = []
        if done.get(sc, 0) < mn:
            bits.append(f"{done.get(sc, 0)} of {mn} minimum searches ran")
        if sc in failed:
            bits.append("a logged search failed")
        if not by_scope.get(sc):
            bits.append("no candidate items in the window")
        if bits:
            nf.append(f"{sc}: {'; '.join(bits)}")
    L += nf or ["none"]
    return "\n".join(L)


def build(rep, item, market, as_of, run_stamp, ledger, prices, comps, divs, holidays, figs, ctx, kept, store, clean,
          harness_text, bing_q):
    """Returns (full_text, data_text). prices: stock bars DataFrame; comps: [(label, kind, DataFrame)]."""
    last_bar = prices["Date"].iloc[-1]
    lr = figs.get("most recent results date")
    lr_line = (f"last_results_date: {lr[0]} (source: {lr[2]}, {lr[3]}, {lr[4]})" if lr and lr[0][:4].isdigit()
               else "last_results_date: NOT SUPPLIED (not in the pipeline's data; find it if you can search)")
    one_year = (date.fromisoformat(last_bar) - timedelta(days=366)).isoformat()
    bars = prices[prices["Date"] > one_year]
    cut = (date.fromisoformat(last_bar) - timedelta(days=365)).isoformat()
    d12 = divs[divs["Date"] > cut] if divs is not None and len(divs) else None
    head = [f"=== RUN INPUTS for the {HARNESS_NAME} - from the user's own GitHub pipeline (fran8c/stock-data), run {run_stamp} ===",
            f"as of: {as_of}", f"ticker: {rep['ticker']}", "mode: A", "depth: STANDARD", "output: STANDARD", lr_line, "",
            f"raw_market_data ({item['symbol']}, {len(bars)} daily bars from Yahoo Finance via the pipeline; Close is the real close, not adjusted):",
            csv_text(bars, ["Date", "Open", "High", "Low", "Close", "Volume"]), ""]
    for label, kind, df in comps:
        head += [f"{kind} ({label}, last {min(COMP_ROWS, len(df))} daily closes, Yahoo Finance via the pipeline):",
                 csv_text(df.tail(COMP_ROWS), ["Date", "Close"]), ""]
    head += ["corporate_actions (dividends and splits, last 12 months, Yahoo Finance via the pipeline):",
             csv_text(d12, ["Date", "Type", "Amount"]) if d12 is not None and len(d12) else "none in the last 12 months", "",
             f"holidays ({market} closures and early closes from the last bar to 3 weeks later, from the pipeline's holidays.txt; "
             f"source: {'SGX trading calendar and MOM gazetted public holidays' if market == 'SGX' else 'NYSE holiday calendar'}):",
             "Date,Type,Name", *(holidays or ["none in this period"]), "",
             "github_ledger:", ledger.strip(), "",
             "news_payload:", news_pack(rep, item, market, as_of, ctx, kept, store, figs, run_stamp, clean, bing_q), "",
             "=== END OF RUN INPUTS ==="]
    inputs = "\n".join(head)
    data_text = (f"Run the {HARNESS_NAME} (saved in this chat or project) on the inputs below. "
                 "If the harness is not saved here, use the full copy file instead.\n\n" + inputs)
    return harness_text.rstrip() + "\n\n" + inputs + "\n", data_text + "\n"
