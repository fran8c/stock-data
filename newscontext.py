"""
Sector, country/macro and global/regional news for the report stocks (decided 21 Nov 2026: GitHub collects,
the AI tops up).

For each stock on report_list.txt this runs the Individual Stock Harness's own fixed searches (news_rules A2)
through Google News RSS, with Bing News RSS as the fallback, and keeps an honest search log:

  COMPANY  C5  "<name> results date OR ex-dividend date <YYYY>"   (C1-C4 are covered by the daily company feeds)
  SECTOR   S1  "<sector words> news <Month YYYY>"
           S2  "<peer 1> <peer 2> <name> shares <Month YYYY>"
  COUNTRY  K1  "<country> stock market <index> <day Month YYYY>"   (latest trading day)
           K2  "<country> <central bank> OR economy OR inflation <Month YYYY>"
           K3  "<central bank> next policy meeting date <YYYY>" and "<country> GDP release date"
  GLOBAL   G1  "global markets central banks oil <Month YYYY>"
           G2  "<region> markets <day Month YYYY>"
  DATE-SPECIFIC (up to 3 big-move days in the window, largest first): "<name> OR <index> <day Month YYYY>"

Searches shared by stocks of one market (K and G) run once per run. Items are only candidates: kept when
dated inside the window, not from a low-quality aggregator, and not a repeat of a kept headline; at most
MAX_PER_SCOPE per scope. The AI applies the harness's keep / discard and transmission-path tests itself.
"""

import time
from datetime import date, timedelta

import pandas as pd

import newsfilter as N

MAX_PER_SCOPE = 6
BIG_CHG, BIG_GAP, BIG_VR = 1.5, 1.0, 1.5

MARKETS = {
    "SGX": {"country": "Singapore", "index": "STI", "index_name": "Straits Times Index", "bank": "MAS",
            "region": "Asia", "exchange": "SGX"},
    "US": {"country": "US", "index": "S&P 500", "index_name": "S&P 500", "bank": "Federal Reserve",
           "region": "Wall Street", "exchange": "US"},
}


def read_report_list(path):
    """report_list.txt lines: SYMBOL | TICKER on EXCHANGE | sector words | peer names (optional, comma-separated)."""
    out = []
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        p = [x.strip() for x in ln.split("|")]
        if len(p) < 3:
            continue
        out.append({"symbol": p[0], "ticker": p[1], "sector": p[2],
                    "peer_names": [x.strip() for x in p[3].split(",") if x.strip()] if len(p) > 3 else []})
    return out


def dstr(d):
    return f"{d.day} {d.strftime('%B %Y')}"


def window(as_of, last_bar):
    """News window: from the earlier of (as_of - 14 days) and the Monday of the first recap week, to as_of."""
    a, lb = date.fromisoformat(as_of), date.fromisoformat(last_bar)
    mon = lb - timedelta(days=lb.weekday())
    first = mon - timedelta(days=7 if lb.weekday() >= 4 else 14)
    return min(a - timedelta(days=14), first).isoformat(), as_of


def big_days(ind, start, end):
    if ind is None or ind.empty:
        return []
    w = ind[(ind["date"] >= start) & (ind["date"] <= end)].copy()
    w = w[(w["chg_pct"].abs() >= BIG_CHG) | (w["gap_pct"].abs() >= BIG_GAP) | (w["vol_ratio"] >= BIG_VR)]
    w = w.reindex(w["chg_pct"].abs().sort_values(ascending=False).index).head(3)
    return [{"date": r["date"], "chg": r["chg_pct"], "gap": r["gap_pct"], "vr": r["vol_ratio"]} for _, r in w.iterrows()]


def queries(rep, item, market, as_of, last_bar, days):
    m = MARKETS[market]
    a, lb = date.fromisoformat(as_of), date.fromisoformat(last_bar)
    month = a.strftime("%B %Y")
    name = item["name"]
    peers = " ".join(rep["peer_names"][:2])
    q = [("COMPANY", "C5", f'"{name}" results date OR ex-dividend date {a.year}', False),
         ("SECTOR", "S1", f"{rep['sector']} news {month}", False),
         ("SECTOR", "S2", f"{peers} {name} shares {month}".strip() if peers else f"{name} peers shares {month}", False),
         ("COUNTRY/MACRO", "K1", f"{m['country']} stock market {m['index']} {dstr(lb)}", True),
         ("COUNTRY/MACRO", "K2", f"{m['country']} {m['bank']} OR economy OR inflation {month}", True),
         ("COUNTRY/MACRO", "K3", f"{m['bank']} next policy meeting date {a.year}", True),
         ("COUNTRY/MACRO", "K3", f"{m['country']} GDP release date {a.year}", True),
         ("GLOBAL/REGIONAL", "G1", f"global markets central banks oil {month}", True),
         ("GLOBAL/REGIONAL", "G2", f"{m['region']} markets {dstr(lb)}", True)]
    for d in days:
        q.append(("DATE-SPECIFIC", f"D {d['date']}", f'"{name}" OR "{m["index_name"]}" {dstr(date.fromisoformat(d["date"]))}', False))
    return q


def run(report, watch_by_sym, ind_dir, market, as_of, fetch, sleep=1.0, log=print):
    """
    report: report_list rows for this run's market. fetch(query, market, days_back) -> list of
    {date, title, source, url, summary}. Returns {symbol: {"window", "log", "items", "big_days", "profile"}}.
    """
    cache, out = {}, {}
    for rep in report:
        sym = rep["symbol"]
        item = watch_by_sym.get(sym)
        if not item:
            continue
        p = f"{ind_dir}/{sym.replace('^', '')}.csv"
        try:
            ind = pd.read_csv(p, usecols=["date", "chg_pct", "gap_pct", "vol_ratio"])
            last_bar = ind["date"].iloc[-1]
        except Exception:  # noqa: BLE001
            ind, last_bar = None, as_of
        start, end = window(as_of, last_bar)
        days = big_days(ind, start, last_bar)
        back = (date.fromisoformat(as_of) - date.fromisoformat(start)).days + 2
        terms = N.company_terms(item)
        logs, kept = [], []
        for scope, code, q, shared in queries(rep, item, market, as_of, last_bar, days):
            key = (q, back)
            status = "ok"
            if key not in cache:
                try:
                    cache[key] = fetch(q, market, back)
                except Exception as e:  # noqa: BLE001
                    cache[key] = None
                    status = f"failed ({e.__class__.__name__})"
                time.sleep(sleep)
            res = cache[key]
            if res is None:
                logs.append({"scope": scope, "code": code, "query": q, "reviewed": 0, "in_window": 0, "status": status or "failed"})
                continue
            inwin = [r for r in res if start <= r["date"] <= end]
            logs.append({"scope": scope, "code": code, "query": q, "reviewed": len(res), "in_window": len(inwin), "status": "ok"})
            for r in sorted(inwin, key=lambda r: r["date"], reverse=True):
                if any(x in (r.get("source") or "").lower() for x in N.LOW_QUALITY):
                    continue
                sc = scope
                if scope == "DATE-SPECIFIC":
                    sc = "COMPANY" if N.about_company(r["title"], terms)[0] else "COUNTRY/MACRO"
                dup = next((k for k in kept if N.similar(k["title"], r["title"]) >= 0.6), None)
                if dup:
                    if (r.get("source") or "") != dup["source"]:
                        dup["sources"].add(r.get("source") or "")
                    continue
                if sum(1 for k in kept if k["scope"] == sc) >= MAX_PER_SCOPE:
                    continue
                kept.append({"scope": sc, "date": r["date"], "title": r["title"], "source": r.get("source") or "Unknown",
                             "url": r.get("url") or "", "summary": r.get("summary"), "via": code,
                             "sources": {r.get("source") or ""}})
        for k in kept:
            k["tag"] = "CORROBORATED" if len(k.pop("sources")) >= 2 else "SINGLE-SOURCE"
        out[sym] = {"window": (start, end), "last_bar": last_bar, "log": logs, "items": kept, "big_days": days}
        log(f"{sym}: context news - {sum(l['status'] == 'ok' for l in logs)} of {len(logs)} searches ran, {len(kept)} candidates kept")
    return out
