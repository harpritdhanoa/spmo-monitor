# SPMO Drift Monitor

Live page: **https://harpritdhanoa.github.io/spmo-monitor/**

Tracks how the constituents of SPMO (Invesco S&P 500 Momentum ETF) have drifted since
the last semi-annual rebalance, and runs a Cheetah-style **rebalance radar** — every
S&P 500 name scored on SPMO's recipe (12-month risk-adjusted momentum, top ~100,
20% buffer, 10% issuer cap) to show which holdings are safe / at risk and which
outsiders would enter if the churn happened today.

Built for replicating SPMO as a manual stock basket in IBKR (UK accounts can't buy
US-domiciled ETFs under PRIIPs). The pie builder on the page renormalises the top-N
weights and exports a CSV for Basket Trader.

## How it updates

`.github/workflows/update.yml` runs every weekday at 22:30 UTC (after the US close):
prices via Yahoo Finance, S&P 500 membership via Wikipedia, then regenerates
`index.html` from `template.html` and commits it.

`data/holdings.json` is the share basis of the basket. It only changes at rebalances
(creations/redemptions scale pro-rata and don't affect weights), so it is refreshed
manually after each churn — last refreshed **19 November 2026** (post the 18 Sep 2026 churn); next churn effective **19 March 2027**.

## Files

- `template.html` — dashboard source; data injected between `/*DATA_START*/` and
  `/*RADAR_START*/` markers.
- `scripts/update.py` — daily job: drift dataset + radar + inject.
- `scripts/engine.py` — the validated SPMO replication engine (universe
  reconstruction, momentum scoring, buffer selection, issuer-capped weights).
- `data/holdings.json` — tickers/names/shares of the current basket.

If the nightly job stops committing, the page shows a red staleness banner after 5 days.
Source for a post-churn refresh: Invesco's SPMO product page holdings table (the CSV
download endpoint refuses scripted requests). SEC N-PORT lags ~2 months behind a churn,
so it cannot supply the new basket in time.

Not investment advice. Momentum strategies crash hard at market turns.
