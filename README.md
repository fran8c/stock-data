# stock-data

A daily job that collects prices, dividends, fundamentals, rates and news for my watchlist and publishes them for my AI harnesses.

- **Runs:** Tuesday to Saturday, about 8:07am Singapore time (after the US close), on GitHub's machines. Also runs straight away whenever `watchlist.txt` is edited.
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

After every run, good or bad, the job posts a comment on the issue **Daily stock data reports** (Issues tab). The comment mentions you, so GitHub emails you. If you use the GitHub mobile app, you also get a phone notification. Each comment has:

- the status (OK, OK with gaps, or FAILED with what's missing)
- a market summary: 1-year rates, each index and stock's last close with 1-day and 1-year change, the biggest movers, and results or ex-dividend dates in the next 14 days
- **latest** links (dashboard, AI bundle, data pack, news pack): these always show the newest run
- **this run's copy** links: frozen copies of that day's files, which never change
- a link to the run log

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
| `pipeline.py` | The script that collects everything |
| `.github/workflows/daily.yml` | The schedule |
| `docs/index.html` | The dashboard page |
| `docs/data/` | The daily output (written by the job, don't edit) |
| `docs/data/archive/` | Files of names taken off the watchlist |

Data comes from Yahoo Finance (unofficial), FRED, MAS, SingStat and Google News. It's for personal research, not investment advice.
