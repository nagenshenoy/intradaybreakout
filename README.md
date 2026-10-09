# Intraday Breakout Scanner (Flask)

A browser-based version of the original Tkinter `IntradayBreakout.py`. It scans a list of
symbols on 5-minute and/or 60-minute candles and flags Bollinger Band breakouts, with RSI,
volume and weekly-level context. The scan logic is unchanged; only the interface moved to the web.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:5000>.

Shortcuts that create a virtualenv and start the app for you: `run.bat` (Windows) or `./run.sh` (macOS/Linux).

1. **Load a stock list**, either way:
   - **Quick load watchlist**: pick one of the four sheets of the bundled `data/ScannerData.xlsx`
     (Nifty50, Nifty500, Smallcap500, NiftyTotalMarket) and click **Load**. No upload needed.
   - **Upload CSV / XLSX**: your own list, symbols in the first column. If a workbook has several sheets,
     a **Select sheet** dropdown appears; choose one and click **Load sheet**. The parsed workbook is kept on
     the server under a token, so switching sheets never needs a re-upload.
   - A header row is **optional and auto-detected** (`Symbol`, `Ticker`, ...). Headerless sheets, like the
     bundled ones, are read from the first row. Use Yahoo Finance tickers, e.g. `RELIANCE.NS`.
2. Pick 5 min and/or 60 min, set the options, click **Start scan** (top bar).
3. Watch the progress bar (**Scanning INFY.NS... 17/50**). Signals appear in the table the moment they are
   found, not at the end of the scan.
4. Filter, sort (click a column header), click a row for details, click a symbol to open TradingView,
   or use the **Export** panel to download a CSV for the active tab.

**Scan setup**, **Filters** and **Export** are collapsible (click the panel title). When collapsed they show
a one-line summary, and the open/closed state is remembered. Start/Stop stay in the sticky top bar either way.

### Export panel

- **Scope**:
  - **Filtered results (as shown in the table)** — exactly what the Filters panel and the active tab are
    currently showing.
  - **All data (this tab, ignoring filters)** — every signal recorded in the active tab, regardless of the
    Filters panel.
  - **Only one Scan#…** — a dropdown of every Scan# that has signals, newest first, each labelled with its
    time and signal count; ignores the Filters panel too.
- **Symbol format**: **With `.NS` suffix** (`ITC.NS`, as stored internally), **Plain symbol** (`ITC`), or
  **With `NSE:` prefix** (`NSE:ITC`) — handy for pasting straight into a TradingView or broker watchlist.

To replace the bundled lists, overwrite `data/ScannerData.xlsx` (it is re-read automatically when the file
changes) or point the `SCANNER_DATA` environment variable at another workbook.

Set `HOST` / `PORT` environment variables to change the bind address (default `127.0.0.1:5000`).

## What was ported

| Tkinter app | Flask app |
|---|---|
| Import CSV/XLSX dialog | Quick-load bundled sheets, upload with sheet picker |
| Fixed scan setup (stop to change it) | **Live setup updates**: change timeframes/lookback/interval/workers while running (see below) |
| 5m / 60m notebook tabs | Pill tabs (now 1m / 5m / 60m) |
| Lookback days, RSI lookback, scan interval | Same controls |
| Filters + "Apply Filters" | Same filters, evaluated server-side; Apply, Enter, or changing a dropdown re-applies them |
| Colour-coded text widget | Sortable table (green = Above Band, red = Below Band) |
| Clear / Export active tab | Same; export honours the active filters |
| Start / Stop | Same (background thread on the server), plus an automatic stop at 3:25 PM IST (see below) |
| High-volume flag | Removed |
| RSI-extreme "RSI>XD" column | Removed, along with the RSI-lookback config option it depended on |
| Flat, ever-growing results list | **Collapsible rows grouped by Scan#** (see below) |

Signal rules, RSI thresholds (80 for 5m, 75 for 60m, below 25 vs daily RSI), weekly / 2-week high-low
comparison, and the "New Signal / Continue N" tracking are the same as before.

### 1-minute timeframe

Tick **1 min scan** under Timeframes to add a 1 Minute tab alongside 5m and 60m. It uses the same rules,
Rel Vol, weekly levels, Filters, Scan# grouping and CSV export as the other tabs, and the lookback setting
(0 = bands only, 1-10 days) works identically. Passes run in the order 1m, 5m, 60m. It is off by default.

Yahoo Finance only provides about 7 calendar days (roughly 5 trading days) of 1-minute data, so a
lookback of 6-10 days on the 1m tab is effectively capped at the history available. A 1-minute pass also
downloads far more candles per symbol, so expect it to be slower on large lists (consider more workers).

### Scan numbers

A Scan# is only used once a scan pass finds at least one signal. A pass with no signals does not consume
a number, so the sequence has no gaps (after scan 66, an empty pass is followed by another pass that is
also called 67, until one produces output). Each timeframe numbers its scans independently, and Clear
restarts a tab at 1. The "Last scan #" tile shows the most recent number that has signals.

### Lookback days (0 = Bollinger bands only)

`Lookback days` accepts **0 to 10** (default 2).

- **1-10**: a breakout needs the close beyond the band *and* beyond the highest high / lowest low of the
  previous N trading days of candles (N x 375 for 1-minute, N x 75 for 5-minute, N x 7 for 60-minute).
- **0 (bands only)**: no lookback comparison at all. **Above Band** = close above the upper Bollinger Band;
  **Below Band** = close below the lower band. Works for the 1-, 5- and 60-minute scans, and any
  candle of the session (including the first) can signal.

Because there is no high/low filter, lookback 0 produces noticeably more signals than 1 or more. Signals are
ordinary results: Filters, Scan# grouping, Continue-N tracking and CSV export treat them like any other.
Lookback can be changed live and applies from the next scan pass. Rows don't record which lookback produced
them, so if you change it mid-run, earlier and later Scan# groups use different rules.

### Rel Vol (replaces the old High Volume flag)

`Rel Vol = average volume of today's <interval> candles (9:15 AM through the current candle)
÷ average volume of the previous full trading day's <interval> candles`

The previous-day average is capped at one day's worth of candles for the interval - 375 minutes of
NSE trading (9:15 AM - 3:30 PM) ÷ the candle length, e.g. 75 candles for 5-minute, 7 for 60-minute.
Rel Vol shows as e.g. `2.20×` (green, above the previous day's average) or `0.45×` (red, below it); a
dash means there isn't a previous trading day in the fetched history to compare against yet. Filter on
it with `>`, `>=`, `<`, `<=` or `=` in the **Rel Vol** filter, in place of the old High Volume dropdown.

### Live Scan Setup updates

Scan Setup no longer needs Stop-then-Start to change. Edit any field (timeframes, lookback days,
scan interval, parallel fetches) while a scan is running and it's pushed to the server immediately.
The engine re-checks its settings **after every scan interval**, right before the next scan pass, and
scans in the usual order (5-minute first, then 60-minute) - so a change never interrupts a pass that's
already in progress; it always takes effect starting with the next one.

### Collapsible rows by Scan#

Results are grouped into a collapsible block per Scan#, newest first, each with a header showing the
scan time and an above/below signal count. Click a group's header to collapse or expand it, or use
**Expand all / Collapse all** above the table. Column sorting still works - it reorders rows within
each group, while the groups themselves always stay ordered by Scan# so recent results stay on top.

### Auto-stop at 3:25 PM IST

The scanner automatically stops itself 5 minutes before the NSE close (3:30 PM IST), so it isn't left
running against a closed market. It only ever checks the time **between** scan passes, never in the
middle of scanning a symbol list, so a scan that's already in progress when 3:25 PM hits is always
allowed to finish before the engine stops. The status pill and a toast both say when this happened
("Stopped (3:25 PM cutoff)").

Only a scan that was actually **started before** 3:25 PM IST is subject to this cutoff. Starting a
scan at any other time of day (evening testing, a different timezone, catching up on a Sunday, ...)
scans normally with no auto-stop at all - it's a stop trigger for a scan that's already running, not
a block on ever starting one outside that window.

The **Time** column (and the Fetch errors timestamps) are also always IST, regardless of which timezone
the server itself is running in - useful since most hosting platforms (including Render) run servers in UTC.

## Small differences from the original

- **Change%** is measured from the open of the *latest session in the data*. The original used
  `datetime.date.today()`, which raised an error (and silently dropped every signal) on weekends and holidays.
- **Errors don't kill the scan.** A failed fetch for one symbol is counted in the "Fetch errors" KPI and the
  scan carries on. The original would crash its scan thread.
- **CSV export** has clean numeric columns (`Week1 %vsLevel`, `PrevWeekHigh`, ...). The original
  wrote a duplicated `LTP` column relabelled `WeekHigh/Low`.
- **Duplicate symbols** are removed, and the header row is auto-detected instead of always being skipped, so a headerless list no longer loses its first symbol.
- **Rel Vol** replaces the old High Volume flag with a real number, the **RSI>XD** column and its
  RSI-lookback setting were removed as unnecessary, and the scanner **auto-stops at 3:25 PM IST** and
  supports **live Scan Setup updates** while running (all above) - the original Tk app had none of these.
- New **Parallel fetches** setting (default 1 = sequential, like the original). Raising it speeds up large
  lists, but too many parallel requests can trigger Yahoo rate limiting.
- The table shows the newest 1000 matching rows; the export always contains all of them.

## Project layout

```
app.py              Flask app + JSON API
sources.py          Bundled workbook, uploads, header detection
data/ScannerData.xlsx   Bundled watchlists (4 sheets)
scanner.py          Scan engine (indicators, breakout rules, filters, background loop) - no Flask dependency
templates/index.html
static/css/         design-tokens.css (dashboard design system) + app.css
static/js/app.js
tests/              pytest suite using a fake data fetcher (no network needed)
sample_symbols.csv
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | Status, live scan progress, counters, config, recent errors |
| GET | `/api/sources` | Bundled sheets (with symbol counts) and the latest multi-sheet upload |
| POST | `/api/load` | JSON: `{"source": "bundled" or <upload token>, "sheet": "Nifty50"}` |
| POST | `/api/import` | Upload symbol list (multipart `file`). Multi-sheet workbooks return `needs_sheet` + a token |
| POST | `/api/scan/start` | JSON: `scan_1m, scan_5m, scan_60m, lookback_days, scan_interval, workers` |
| POST | `/api/scan/config` | Same JSON as `/api/scan/start`; updates settings for a scan already running, applied from the next scan pass |
| POST | `/api/scan/stop` | Stop after the current symbol |
| POST | `/api/clear` | JSON: `{"tab": "5m"}` |
| GET | `/api/results` | Filtered rows, query: `tab, symbol, band, change_op, change_val, rsi_op, rsi_val, action_types, high_vol, week1, week2, scan_from, scan_to` |
| GET | `/api/scans` | Distinct Scan# values for a tab (`tab`), newest first, each with its time and signal count — powers the Export panel's Scan# dropdown |
| GET | `/api/export` | Same query as `/api/results`, plus `symbol_format` (`ns` \| `plain` \| `nse`); returns CSV |

## Deploying on Render

This package is ready to deploy on [Render](https://render.com) as a Web Service:

1. Push this folder to a GitHub/GitLab repo (or use Render's "Public Git Repository" option pointing at it).
2. In Render, choose **New > Blueprint** and point it at the repo — it will pick up `render.yaml`
   automatically and configure the build/start commands for you. Alternatively choose **New > Web Service**
   manually with:
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:$PORT app:app`
3. Render sets `$PORT` automatically; no other environment variables are required. `SCANNER_DATA` can be
   set if you want to point at a different bundled workbook.

Notes specific to hosting this app remotely:

- Keep it to **one web worker** (`-w 1`, already set above) — scan state lives in memory in a single process.
  Render's free/starter plans don't autoscale a single service to multiple instances, so this is a non-issue
  there, but don't manually add more instances of this service.
- This is a single-user tool with **no authentication**. Anyone who reaches the public Render URL can start/stop
  scans and see the data. Put it behind Render's built-in basic auth, an IP allowlist, or a reverse proxy that
  adds auth if that matters for your use case.
- Free-tier Render web services spin down after inactivity and take ~30-60s to wake back up on the next
  request — the first load after idling will be slow.
- `Procfile` and `runtime.txt` are also included for Heroku-style platforms that read those files instead of
  `render.yaml`.

## Notes

- Scan state lives **in memory in one process**. Run a single worker (`python app.py`, or
  `gunicorn -w 1 --threads 8 app:app`). It is a single-user tool with no authentication, so keep it bound to
  localhost unless you put it behind something that adds auth.
- Data comes from Yahoo Finance through `yfinance` and may be delayed. This is not investment advice.
- Run the tests with `pip install pytest && pytest`.
