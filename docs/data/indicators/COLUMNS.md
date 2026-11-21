# Indicator columns (one row per trading day)

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
range calibration and comparator dates. Trend charts: see the dashboard.
