# stock-data

A daily job that collects prices, dividends, fundamentals, rates and news for my watchlist and publishes them for my AI harnesses.

- **Runs:** two a day on GitHub's machines, each about 5 hours after its market closes: **SGX run** 22:07 Singapore time, Monday to Friday; **US run** 10:07 Singapore time, Tuesday to Saturday. Each run collects only its own market and keeps the other market's figures from its last run. Also runs (both markets) straight away whenever `watchlist.txt` or `peers.txt` is edited. To run by hand: Actions tab -> Daily stock data -> Run workflow -> pick `both`, `SGX` or `US`.
- **Dashboard:** `https://<your-username>.github.io/stock-data/`
- **Files for the AI:** `https://<your-username>.github.io/stock-data/data/ai_bundle.txt` (everything in one file)

## One-time setup (all in the browser, nothing to install)

**1. Create the repo**
Go to github.com/new. Name it `stock-data`, choose **Public**, leave everything else unticked, and click **Create repository**.

**2. Turn on the website (before uploading)**
Go to **Settings → Pages**. Under **Build and deployment → Source**, choose **GitHub Actions**.

**3. Upload the files**
On the empty repo page, click **uploading an existing file**. Unzip `stock-data.zip` on your computer, go inside the `stock-data` folder, select everything (including the `.github` and `docs` folders), and drag it into the browser. Click **Commit changes**.

Check that `.github/workflows/daily.yml` is now in the repo. If it isn't, click **Add file → Create new file**, type `.github/workflows/daily.yml` as the name, paste in the contents of that file, and commit.

**4. Watch the first run**
The upload includes `watchlist.txt`, so a run starts by itself. Go to the **Actions** tab to watch it. The first run takes 10–20 minutes because it reads a month of news. Later runs take about 5 minutes.

A green tick means it worked. A red cross means something stopped it. Click the run, then the step with the red cross, to read why. If no run started, click **Daily stock data** on the left, then **Run workflow → Run workflow**.

**5. Check the result**
Open `https://<your-username>.github.io/stock-data/`. The banner at the top says what was collected and what's missing.

**6. Make sure failure emails reach you**
Go to github.com/settings/notifications. Under **Actions**, tick **Email** and **Only notify for failed workflows**.

## Day to day

- **Change the watchlist:** click **Edit watchlist** on the dashboard (or open `watchlist.txt` on GitHub and click the pencil icon), edit, and commit. A run starts straight away; new data shows about 10 minutes later.
  - **Added names** arrive complete: a full year of prices, dividends, fundamentals and 30 days of news.
  - **Removed names** have their files moved to `docs/data/archive/`, with a dated line in `archive_log.txt`. They drop off the dashboard and the AI files.
  - **Re-added names** download fresh history; the old archived copy is cleared.
- **Run it now:** Actions tab → Daily stock data → Run workflow.
- **Give it to an AI:** use the buttons under "For your AI" on the dashboard.

## Daily report notice

After every run (SGX and US), good or bad, the job posts a comment on the issue **Daily stock data reports** (Issues tab). The comment mentions you, so GitHub emails you. If you use the GitHub mobile app, you also get a phone notification. Each comment has:

- which run it was (SGX run / US run) and its status (OK, OK with gaps, or FAILED with what's missing)
- any files still behind after the re-pulls
- a market summary: 1-year rates, each index and stock's last close with 1-day and 1-year change, the biggest movers, and results or ex-dividend dates in the next 14 days
- **latest** links (dashboard, AI bundle, data pack, news pack): these always show the newest run
- **this run's copy** links: frozen copies of that day's files, which never change
- a link to the run log

## Indicator ledger (each stock's calculations, every day)

Every run works out all the Individual Stock Harness indicators for every trading day of the past year, from up to 5 years of Yahoo bars. These are the **official figures**: the harness checks against them and flags any difference. Files in `docs/data/indicators/`, per stock:

- `<SYMBOL>.csv` - one row per day: order flow (CLV, CVD10, VWAP10, UDVR10, volume ratio, VT5/20), ATR14, MACD, Ichimoku, Darvas, 20-day relative strength vs each benchmark and peer, 20/60/120-bar and 52-week highs and lows, confluence and volume confirmation, outlook ranges, nearest support and resistance. Open it on github.com to see it as a searchable table.
- `<SYMBOL>_weeks.csv` - one row per week (the weekly recap figures).
- `<SYMBOL>_ledger.txt` - the latest day as a readable ledger (the harness's Appendix B layout).
- `<SYMBOL>_latest.json` - the latest day in full, for the dashboard and the harness.
- `COLUMNS.md` - what every column means, in plain English.

A frozen copy of each day's latest figures is saved in `docs/data/history/YYYY-MM-DD/indicators/`. Past rows are recalculated each run, so if Yahoo corrects an old bar, the history picks up the correction. The figures were checked against the 21 Nov 2026 harness runs for DBS, OCBC, Keppel, Marvell and Nvidia (174 of 174 match to 4 decimals).

## Stock pages (charts of every figure)

Each stock card on the dashboard has a **Charts & figures** button that opens `stock.html?s=<SYMBOL>`. The page shows the latest official figures and action, then charts with a hover readout. Charts cover: price with VWAP10, Kijun and the Ichimoku cloud; expected range and nearest levels; ATR14; 52-week position; CVD10; UDVR10; volume ratio; VT5/20; MACD; Ichimoku score; 20-day relative strength vs each benchmark and peer; the Darvas box; confluence; and the pillar reading counts. You can also pick **any** column to chart, see today's pillar readings, and see the full daily table. Period buttons: 1M, 3M, 6M, 1Y. It works in light and dark mode and needs no extra software (plain HTML, no outside libraries).

## Pillar readings (statistics, not advice)

GitHub provides statistics and news only. It sets **no** entries, stops, targets or buy/sell labels; the AI harness does all trade reasoning. Each run records, for every day, the readings behind the harness's pillars as plain counts: microstructure (4 readings), momentum (3 readings), structure facts (Darvas status, distance to the nearest support and resistance in % and ATRs) and broker rating changes in the last 14 days. Files in `docs/data/indicators/`: `<SYMBOL>_scores.csv` (one row per day), and a SCORES section in `<SYMBOL>_ledger.txt` and `<SYMBOL>_latest.json`. The readings were checked against the 21 Nov 2026 harness runs for DBS, OCBC, Keppel, Marvell and Nvidia.

## News pack (short, price-relevant)

The job still reads 30 days of news (Google News, Bing News, Yahoo) into `news_store.json`. For the harness it then keeps, per stock, only:

- items **about the company itself**: its name in the headline; not research the company publishes about others (e.g. "OCBC keeps BUY on Seatrium"), not an affiliate trust or REIT, not one name in a list;
- **price-relevant event types**: results, guidance, rating changes (maintained calls and target-only changes are dropped), deals and contracts, dividends / buybacks / share or bond issues, regulators and legal, management changes, index changes;
- the **14 days** to the stock's last bar, at most **15 items**, ranked by relevance (big-move days and better sources first; repeat coverage merged).

Each item shows its trading day's change %, gap % and volume ratio, with **BIG MOVE** when the change is at least 1.5%, the gap at least 1.0% or volume at least 1.5x normal. Each stock also lists **big-move days with no news kept**, so you know where to look. The rules are keyword-based, so check an item before relying on it. The full unfiltered list is in `news_all.txt`. Sector, country and global news are not in the pack: the harness searches for those itself.

## Website publishing

If publishing the website fails (GitHub sometimes can't find the website package for a few seconds), the job waits a minute and tries once more. The data is saved to the repo before publishing, so a publish failure never loses data.

## Late bars and STALE alerts

Yahoo sometimes publishes a day's bar late. Each run checks every price file against its market's latest session (weekends and the closures in `holidays.txt` are skipped; early-close days still count as sessions). Any file that is behind is re-pulled up to 2 more times, 15 minutes apart. If it is still behind, the job opens an issue titled like **STALE: MRVL, NVDA behind (US run)**, which emails you. The next run of that market closes the issue once the files catch up. No issue means everything is up to date.

Every run rewrites each price file in full, so Yahoo's later corrections to recent bars (e.g. volume) are picked up too.

**Each year:** add the next year's market holidays to `holidays.txt` (MOM publishes Singapore's in June; NYSE publishes in December). A missing holiday causes a false STALE alert that day.

## Past days

- **Past days page:** `https://<your-username>.github.io/stock-data/history.html` (also linked at the top of the dashboard). Pick a date to see that day's status and summary, with links to its data and news packs.
- **Dated folders:** `docs/data/history/YYYY-MM-DD/` holds each day's status, summary, data pack and news pack (about 0.3 MB a day).
- **Your inbox:** each email's "this run's copy" links open that exact day's files.

Make sure github.com/settings/notifications has **Email** ticked under "Participating, @mentions and custom". Keep the issue open; if you close it, the next run starts a new one.

## What counts as a failure (you also get GitHub's own failure email)

- Prices missing for any watchlist name or benchmark (STI, S&P 500, Nasdaq, Dow), or a price more than 5 days old.
- The US 1-year Treasury yield or the Singapore 1-year T-bill yield missing.

Everything else that can't be fetched is marked `NOT RETRIEVED` and listed in the dashboard banner. No email is sent for those.

## Known gaps

| Figure | Why |
|---|---|
| CET1 and its minimum (banks) | Not on Yahoo; comes from each bank's Pillar 3 disclosure |
| ISM and SIPMM manufacturing PMI | Licensed or published only as press releases |
| Singapore GDP growth, MAS core inflation | Feeds still to be connected |
| 5-year P/E or P/B range | Approximate, and Yahoo only has about 4 years of annual figures |
| YZJ (BS6.SI) ratios | Its accounts are in CNY while the price is in SGD |
| News summaries for paywalled outlets | The outlet only shows a teaser or blocks automated visits |

## Files

| Path | What it is |
|---|---|
| `watchlist.txt` | The names to track. Edit this one. |
| `peers.txt` | Two peers per stock for the Individual Stock Harness |
| `holidays.txt` | Market closures and early closes (for the stale check and the outlook ranges) |
| `pipeline.py` | The script that collects everything |
| `indicators.py` | Works out the indicator ledger (called by `pipeline.py`) |
| `newsfilter.py` | Builds the short, price-relevant news pack (called by `pipeline.py`) |
| `pillars.py` | Works out the pillar readings as statistics (called by `pipeline.py`) |
| `.github/workflows/daily.yml` | The schedule |
| `docs/index.html` | The dashboard page |
| `docs/data/` | The daily output (written by the job, don't edit) |
| `docs/data/parts/` | Each market's latest results, combined into the packs |
| `docs/data/indicators/` | The indicator ledger, per stock |
| `docs/data/archive/` | Files of names taken off the watchlist |

Data comes from Yahoo Finance (unofficial), FRED, MAS, SingStat and Google News. It's for personal research, not investment advice.
