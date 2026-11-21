"""
Short, price-relevant news pack (decided 21 Nov 2026).

From the 30-day news store, for each watchlist stock it keeps only items that:
  1. are about the company itself: its name or ticker is in the headline, it is
     not just one name in a list, and it is not research the company publishes
     about other companies or currencies (e.g. "OCBC keeps BUY on Seatrium",
     "Rupiah ... - OCBC") or news about an affiliate trust or REIT;
  2. are a price-relevant event type: RESULTS, GUIDANCE, RATING CHANGE, DEAL
     (M&A, stakes, contracts, partnerships), CAPITAL (dividends, buybacks, share
     or bond issues), REGULATORY / LEGAL, MANAGEMENT, INDEX. Commentary, opinion,
     price forecasts, CSR and marketing are dropped. Maintained ratings and
     target-only changes are dropped (rating changes and initiations are kept);
  3. fall in the 14 days to the stock's last bar (plus any weekend news after it).
Each kept item is tagged with its trading day's change %, gap % and volume
ratio (from the indicator ledger). Days with a big move (|change| >= 1.5%,
|gap| >= 1.0% or volume >= 1.5x normal, the harness's date-specific rule) rank
higher, and big-move days with no kept item are listed so the reader knows
where news is missing. Similar headlines are merged. At most MAX_ITEMS per stock,
ranked by relevance.
Rules are keyword-based and err on the side of dropping noise; the full 30-day
list stays in news_all.txt for anyone who wants to check what was dropped.
"""

import math
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

LOW_QUALITY = ("ad-hoc-news", "minichart", "kucoin", "simply wall st", "marketbeat", "stocktitan", "aastocks", "富途", "moomoo")
PER_DAY_EVENT = 2    # at most this many items of one event type for one trading day (repeat coverage)
WINDOW_DAYS = 14
MAX_ITEMS = 15
BIG_CHG, BIG_GAP, BIG_VR = 1.5, 1.0, 1.5

GENERIC = {"ltd", "limited", "group", "holdings", "holding", "corp", "corporation", "inc", "plc", "co",
           "technology", "technologies", "industries", "the", "company", "bank", "shipbuilding"}

EVENTS = [  # (type, pattern, weight) - first match wins
    ("GUIDANCE", r"\b(upgrad\w*|rais\w*|cut\w*|lower\w*|lift\w*|trim\w*) (its |\d{4} |full-year |fy |annual )*(guidance|outlook|forecast|targets?)\b", 8),
    ("RATING CHANGE", r"\b(upgrad\w*|downgrad\w*|initiat\w* (coverage|at|with)|cut\w* to (sell|hold|neutral|underweight|underperform)|"
                      r"rais\w* to (buy|outperform|overweight)|lift\w* to (buy|outperform|overweight))\b", 9),
    ("REGULATORY / LEGAL", r"\b(sec|regulator\w*|antitrust|probe\w*|investigat\w*|lawsuit\w*|sued|sues|fine[ds]?|penalt\w*|"
                           r"attorney general|subpoena\w*|export (control|curb|rule|ban|restriction)\w*|sanction\w*|licen[cs]e\w*|"
                           r"court|ruling|settle\w* (lawsuit|case|charges|claims|probe)|settlement with (the )?(sec|regulators?|authorities|doj|ftc)|"
                           r"class action|mas (fines|penal|reprimand|action))\b", 9),
    ("RESULTS", r"\b((quarterly|quarter|q[1-4]|annual|full-year|half-year|interim|h[12]|financial|earnings|fiscal \w+ quarter) results|"
                r"results (beat|miss|top|date|on|due)|report\w* (\w+ )?(results|earnings)|to report|earnings (beat|miss|report|release|call|surprise|date)|"
                r"q[1-4]\b|[1-4]q\d{0,2}\b|[12]h\d{0,2}\b|fy\d{2,4}|nine-month|9m|first[- ]half|second[- ]half|net profit|net income|"
                r"profit (rises|falls|jumps|drops|up|down|surges|slumps)|revenue (rises|falls|jumps|surges|grows|growth|up|down|beat|miss|target)|"
                r"\d+% revenue|business update|beats? (estimates|forecasts|expectations)|miss(es|ed)? (estimates|forecasts|expectations))\b", 8),
    ("GUIDANCE", r"\b(guidance|outlook (raised|cut|lowered|upgrade)|rais\w* (outlook|forecast|guidance)|cut\w* (outlook|forecast|guidance)|"
                 r"lower\w* (outlook|forecast|guidance)|profit warning|warns? (on|of) (profit|earnings|revenue|sales|demand))\b", 8),
    ("MANAGEMENT", r"\b(resign\w*|steps? down|appoint\w*|named (as )?(ceo|cfo|chair\w*|president|chief)|names (new )?(ceo|cfo|chair\w*|president|chief)|"
                   r"succe(ed|eds|eded|ssor|ssion)|retir(e|es|ing|ement)|ousted|oust|fired|interim (ceo|cfo|chief|chair\w*)|"
                   r"(ceo|cfo|chairman|chief executive)'?s? (exit|departure|resignation)|reinstat\w*|new (ceo|cfo|chair\w*|directors?)|"
                   r"adds? (three |two |\w+ )?(new )?(independent )?directors?|board (shake-?up|changes?|expands|appoints)|insider|form 4|"
                   r"sells? \$?[\d.,]+\s*(m|million|bn|billion|k)?\s*(in |of |worth of )?shares|buys? \$?[\d.,]+\s*(m|million|bn|billion|k)?\s*(in |of |worth of )?shares)\b", 8),
    ("CAPITAL", r"\b(dividend\w*|buy-?back\w*|repurchase\w*|share (sale|placement|issue|offering)|placement|rights issue|"
                r"bonds?|notes offering|debt offering|perpetual|capital (return|raising)|stock split|share split|"
                r"secondary offering|ipo approval|ipo (plans?|filing|priced|pricing|launch)|files? for (an )?ipo|its ipo|listing of|"
                r"lock-?up (expiry|expiration|ends)|share unlock)\b", 7),
    ("DEAL", r"\b(acquir\w*|acquisition\w*|merger\w*|merge\w*|takeover\w*|buy\w* stake|sell\w* stake|stake (in|sale)|divest\w*|"
             r"dispos\w*|deal|deals|contract\w*|order\w* (worth|for|from)|wins? (order|contract)|partnership|partners with|"
             r"joint venture|tie-?up|agreement|mou|memorandum|partner(s|ed)? (on|to))\b", 6),
    ("INDEX", r"\b(index (inclusion|addition|deletion|rebalanc\w*|review)|added to the|removed from the|joins? the (s&p|nasdaq|msci|sti))\b", 6),
]

DROP = [  # commentary, opinion, forecasts, CSR, marketing, page types
    r"seasonal returns? chart", r"technical outlook", r"week ahead", r"price (forecast|prediction|target forecast)",
    r"\bshould (i|you)\b", r"\bbuy the dip\b", r"\bis it (time|too late)\b", r"^why\b", r"^how\b", r"^what\b", r"^here'?s\b",
    r"\b(\d+|three|two|four|five|ten) (stocks?|reasons?|things?|singapore|blue chips?|temasek|dividend)\b",
    r"\b(donat\w*|charity|scholarship|students|volunteer\w*|sustainab\w*|esg|award\w*|wins? .*award|recogni[sz]ed|"
    r"campaign|sponsor\w*|launch\w* (card|app|campaign|promotion)|promo\w*|cashback|rewards?)\b",
    r"\b(stocks? to watch|hot stocks|top (gainers|losers)|movers)\b", r"\bvs\.?\b", r"\bor\b.*\?$", r"\?$",
    r"\b(opinion|column|podcast|video|newsletter|market hang|live:|live updates)\b",
    r"\banalyst estimates\b", r"\bwhat .* think\b", r"\bfair value\b", r"\b(keep|keeps|maintain\w*|reiterat\w*)\b",
    r"\b(phishing|scam\w*|fraudulent websites?)\b",
    r"\b(still a buy|is a buy|a buy now|buy now|bull case|bear case|game changer)\b", r"^learn\b",
    r"\bcould (be|show|change|see|make|turn|go|soar|produce)\b", r"\bor [\w .&-]+\?", r"\band others\b",
    r"^prediction\b", r"\bhere'?s what i'?m doing\b", r"\bcathie wood\b", r"\$[\d,]+ in\b", r"\bportfolio impact\b",
    r"\b(top|biggest) q[1-4] (\d{4} )?move\b", r"\(rating (downgrade|upgrade)\)", r"\b(catalysts?|reasons?) (set )?to\b",
    r"\bhistory says\b", r"^forget\b", r"\bi'?m (still )?(bullish|bearish|buying|selling)\b", r"\bnot adding\b", r"\b(1|one) number explains\b",
    r"\blessons?\b", r"\bwhat investors should\b", r"\bunusual (trades|options|activity)\b", r"\binstitutional flow\b", r"\bsignal from\b",
    r"\btarget price\b(?!.*(upgrad|downgrad))", r"\bprice target\b(?!.*(upgrad|downgrad))",
]

AFFILIATE = r"\b{name}\s+(dc\s+reit|reit|infrastructure trust|pacific oak|trust|china trust|ultra|logistics trust)\b"


TICKERS = set()   # ticker-only terms; filled by company_terms()
OTHER_NAMES = {"nvidia", "amd", "broadcom", "intel", "micron", "marvell", "palantir", "spacex", "tesla", "apple", "microsoft",
               "meta", "alphabet", "google", "amazon", "anthropic", "openai", "coreweave", "seagate", "western digital",
               "qualcomm", "tsmc", "arm", "oracle", "dbs", "ocbc", "uob", "keppel", "sembcorp", "singtel", "starhub",
               "yangzijiang", "seatrium", "st engineering", "capitaland", "jardine"}


def company_terms(item):
    """Words that identify the company in a headline: name (minus generic words), ticker, quoted search phrases."""
    sym = item["symbol"]
    terms = {sym.split(".")[0].lower()} if not sym[0].isdigit() and not re.match(r"^[A-Z]\d", sym) else set()
    TICKERS.update(terms)
    words = [w for w in re.split(r"[^\w&]+", item["name"]) if w and w.lower() not in GENERIC]
    if words:
        terms.add(" ".join(words).lower())
        terms.add(words[0].lower())
    for q in re.findall(r'"([^"]+)"', item.get("query", "")):
        qw = [w for w in re.split(r"[^\w&]+", q) if w and w.lower() not in GENERIC]
        if qw:
            terms.add(" ".join(qw).lower())
    return {t for t in terms if len(t) >= 3}


def about_company(title, terms):
    t = title.lower()
    hit = [x for x in terms if re.search(rf"\b{re.escape(x)}\b", t)]
    names = [x for x in hit if x not in TICKERS]
    if not names and not (hit and re.search(r"\b(stock|shares|nasdaq|nyse|sgx)\b", t)):
        return False, "company not in headline"
    name = max(hit, key=len)
    n = re.escape(name)
    if re.search(AFFILIATE.format(name=n), t):
        return False, "affiliate (trust/REIT), not the company"
    # The company acting as researcher / commentator on others
    research = [
        rf"\b{n}\s+(says|keeps|maintains|rates|reiterates|initiates|sees|expects|forecasts|tips)\b.*\b(on|for|fair value|target|buy|hold|sell|add|accumulate|reduce)\b",
        rf"[-–—]\s*{n}\s*(group research|research|securities|bank)?\s*$",
        rf"\b{n}\s+(strategists?|economists?|analysts?|research|investment research|securities|global markets|treasury research|wealth management says)\b",
        rf"\b(according to|says|said|notes|via)\s+{n}\b",
        rf"^{n}(\s+(bank|group|research|securities))?\s+(sees|expects|forecasts|predicts|tips|warns that|views)\b",
        rf"\b{n}(\s+(bank|group|group research|research|securities|vickers))?\s+(upgrades|downgrades|initiates|cuts|raises|lowers|lifts|trims|starts)\b",
        rf"\b(as|after|by)\s+{n}(\s+(bank|group|research|securities))?\s+(lowers|raises|cuts|lifts|keeps|maintains|trims|upgrades|downgrades)\b",
    ]
    if any(re.search(p, t) for p in research):
        return False, "research or commentary by the company about others"
    # One name in a list of many
    if t.count(",") >= 2 and re.search(r"(stocks|shares|watch|movers|gainers|losers|:)", t):
        return False, "mentioned in passing (list of names)"
    others = [o for o in OTHER_NAMES if o not in terms and not any(o in x or x in o for x in terms)
              and re.search(rf"\b{re.escape(o)}\b", t)]
    if len(others) >= 2:
        return False, "mentioned in passing (list of names)"
    return True, ""


def classify(title, summary=""):
    t = title.lower()
    for p in DROP:
        if re.search(p, t):
            # an explicit rating change or results headline overrides generic drops like "price target"
            if not re.search(EVENTS[0][1], t):
                return None, 0, "commentary / opinion / other"
    for kind, pat, w in EVENTS:
        if re.search(pat, t):
            return kind, w, ""
    return None, 0, "no price-relevant event type"


def day_reaction(ind, d):
    """The item's trading day: the item date if it is a bar, otherwise the next bar."""
    if ind is None or ind.empty:
        return None
    later = ind[ind["date"] >= d]
    if later.empty:
        return {"day": None, "note": "reaction not yet observable"}
    r = later.iloc[0]
    big = (abs(r["chg_pct"]) >= BIG_CHG if not pd.isna(r["chg_pct"]) else False) or \
          (abs(r["gap_pct"]) >= BIG_GAP if not pd.isna(r["gap_pct"]) else False) or \
          (r["vol_ratio"] >= BIG_VR if not pd.isna(r["vol_ratio"]) else False)
    return {"day": r["date"], "chg": r["chg_pct"], "gap": r["gap_pct"], "vr": r["vol_ratio"], "big": bool(big)}


def similar(a, b):
    stop = set("the a an and or of to in on for with at by from as is are be was were its it this that after over into amid".split())
    wa = {w for w in re.findall(r"[a-z0-9]+", a.lower()) if w not in stop}
    wb = {w for w in re.findall(r"[a-z0-9]+", b.lower()) if w not in stop}
    return len(wa & wb) / max(1, min(len(wa), len(wb)))


def fmt_day(rx):
    if not rx or not rx.get("day"):
        return "day: reaction not yet observable"
    f = lambda x, s="": "n/a" if x is None or pd.isna(x) else f"{x:+.2f}{s}"
    vr = "n/a" if pd.isna(rx["vr"]) else f"{rx['vr']:.2f}x"
    return f"day {rx['day']}: chg {f(rx['chg'], '%')} gap {f(rx['gap'], '%')} vol {vr}" + (" BIG MOVE" if rx["big"] else "")


def build_pack(store, watch, ind_dir, run_date, clean, no_summary):
    """
    store: the news store (dict). watch: watchlist items. ind_dir: folder with <SYMBOL>.csv indicator files.
    Returns (pack_text, kept_by_symbol, counts).
    """
    sections, kept_all, n_sum, n_head, dropped_total = [], {}, 0, 0, {}
    by_t = {}
    for n in store.values():
        by_t.setdefault(n["ticker"], []).append(n)
    for item in watch:
        sym = item["symbol"]
        p = Path(ind_dir) / f"{sym.replace('^', '')}.csv"
        ind = pd.read_csv(p, usecols=["date", "chg_pct", "gap_pct", "vol_ratio"]) if p.exists() else None
        last_bar = ind["date"].iloc[-1] if ind is not None and len(ind) else run_date
        start = (date.fromisoformat(last_bar) - timedelta(days=WINDOW_DAYS)).isoformat()
        items = [n for n in by_t.get(sym, []) if start <= n["date"] <= run_date]
        terms = company_terms(item)
        cands, drops = [], {}
        for n in items:
            ok, why = about_company(n["title"], terms)
            if not ok:
                drops[why] = drops.get(why, 0) + 1
                continue
            kind, w, why = classify(n["title"], n.get("summary") or "")
            if not kind:
                drops[why] = drops.get(why, 0) + 1
                continue
            rx = day_reaction(ind, n["date"])
            score = w + {"PRIMARY": 3, "CORROBORATED": 2}.get(n.get("tag"), 0) + (1 if n.get("summary") else 0) \
                + (2 if rx and rx.get("big") else 0) \
                - (2 if any(q in (n.get("source") or "").lower() for q in LOW_QUALITY) else 0)
            cands.append({**n, "event": kind, "score": score, "rx": rx})
        # merge similar headlines (same story within 2 days): keep the best, count the rest
        cands.sort(key=lambda x: (x["score"], x["date"]), reverse=True)
        merged = []
        for cnd in cands:
            dup = next((m for m in merged if abs((date.fromisoformat(m["date"]) - date.fromisoformat(cnd["date"])).days) <= 2
                        and similar(m["title"], cnd["title"]) >= 0.5), None)
            if dup:
                dup["also"] = dup.get("also", 0) + 1
                if not dup.get("summary") and cnd.get("summary"):
                    dup["summary"] = cnd["summary"]
            else:
                merged.append(cnd)
        slots, diverse = {}, []
        for m in merged:
            key = (m["event"], (m.get("rx") or {}).get("day") or m["date"])
            slots[key] = slots.get(key, 0) + 1
            if slots[key] <= PER_DAY_EVENT:
                diverse.append(m)
        n_merged = len(cands) - len(diverse)
        merged = diverse
        top = merged[:MAX_ITEMS]
        cut = len(merged) - len(top)
        top.sort(key=lambda x: x["date"], reverse=True)
        kept_all[sym] = top
        # big-move days with no kept item
        covered = {k["rx"]["day"] for k in top if k.get("rx") and k["rx"].get("day")}
        quiet = []
        if ind is not None:
            w = ind[(ind["date"] >= start) & (ind["date"] <= last_bar)]
            for _, r in w.iterrows():
                big = abs(r["chg_pct"]) >= BIG_CHG or abs(r["gap_pct"]) >= BIG_GAP or r["vol_ratio"] >= BIG_VR
                if big and r["date"] not in covered:
                    quiet.append(f"{r['date']} (chg {r['chg_pct']:+.2f}%, gap {r['gap_pct']:+.2f}%, vol {r['vol_ratio']:.2f}x)")
        lines = [f"## {sym} - {item['name']} | window {start} to {run_date} (last bar {last_bar}) | "
                 f"kept {len(top)} of {len(items)} items"]
        drop_txt = "; ".join(f"{v} {k}" for k, v in sorted(drops.items(), key=lambda x: -x[1]))
        extra = []
        if n_merged:
            extra.append(f"{n_merged} merged as repeat coverage")
        if cut:
            extra.append(f"{cut} lower-ranked items over the {MAX_ITEMS}-item cap")
        lines.append(f"Dropped: {drop_txt or 'none'}" + (f"; {'; '.join(extra)}" if extra else ""))
        for k in top:
            facts = k.get("summary") or no_summary
            also = f" (+{k['also']} similar report{'s' if k['also'] > 1 else ''})" if k.get("also") else ""
            lines.append(" | ".join(clean(x) for x in (k["date"], k["event"], k["title"] + also, k["source"], k["url"],
                                                       k.get("tag", ""), fmt_day(k.get("rx")), facts)))
            if k.get("summary"):
                n_sum += 1
            else:
                n_head += 1
        if not top:
            lines.append("No price-relevant company news kept in the window.")
        lines.append("BIG-MOVE DAYS WITH NO NEWS KEPT: " + ("; ".join(quiet) if quiet else "none"))
        sections.append("\n".join(lines))
        for k, v in drops.items():
            dropped_total[k] = dropped_total.get(k, 0) + v
    return sections, kept_all, {"with_summary": n_sum, "headline_only": n_head, "dropped": dropped_total}
