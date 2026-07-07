#!/usr/bin/env python3
"""Daily SPMO monitor update.

Re-prices the committed holdings basket (data/holdings.json), rebuilds the
weight-drift dataset since the last churn and the rebalance radar (full S&P 500
scored on SPMO's recipe), and injects both JSON blobs into template.html ->
index.html.

Holdings shares only change at rebalances (creations/redemptions scale
pro-rata and don't affect weights), so this job needs no holdings feed —
refresh data/holdings.json manually after each churn.
"""
import json, sys, time, calendar
from datetime import date, timedelta
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import engine as eng

# ---------- rebalance calendar: effective after close of 3rd Friday Mar/Sep ----------
def third_friday(y, m):
    c = calendar.Calendar()
    fridays = [d for d in c.itermonthdates(y, m) if d.month == m and d.weekday() == 4]
    return fridays[2]

def rebal_dates(today):
    eff = sorted(third_friday(y, m) for y in range(today.year - 1, today.year + 2)
                 for m in (3, 9))
    last = max(d for d in eff if d <= today)
    nxt = min(d for d in eff if d > today)
    return last, nxt

TODAY = date.today()
REBAL, NEXT_REBAL = rebal_dates(TODAY)
print(f"cycle: {REBAL} -> {NEXT_REBAL}", file=sys.stderr)

# ---------- holdings ----------
hj = json.load(open(ROOT / "data" / "holdings.json"))
HOLD = {h["ticker"]: (h["name"], h["shares"]) for h in hj["holdings"]}
incumbents = set(HOLD)

# ---------- universe ----------
uni = eng.sp500_point_in_time(TODAY.strftime("%Y-%m-%d"))
names = {r.Symbol: r.Name for r in uni.itertuples()}
tickers = sorted(set(uni["Symbol"]) | incumbents)
print("universe:", len(uni), "| tickers to price:", len(tickers), file=sys.stderr)

# ---------- prices (with retries for CI flakiness) ----------
def download(tickers, start):
    import yfinance as yf
    out = []
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        for attempt in range(4):
            try:
                d = yf.download(batch, start=start, auto_adjust=True,
                                progress=False, threads=True)
                px = d["Close"] if isinstance(d.columns, pd.MultiIndex) else d
                if px.dropna(how="all").empty:
                    raise RuntimeError("empty batch")
                out.append(px)
                break
            except Exception as e:
                if attempt == 3:
                    raise
                time.sleep(15 * (attempt + 1))
        time.sleep(2)
    px = pd.concat(out, axis=1)
    px.index = pd.to_datetime(px.index)
    return px.sort_index().ffill()

start = (pd.Timestamp(TODAY) - pd.DateOffset(years=3, months=8)).strftime("%Y-%m-%d")
px = download(tickers, start)
missing = [t for t in incumbents if t not in px.columns or px[t].isna().all()]
if missing:
    print("WARNING missing incumbent prices:", missing, file=sys.stderr)
REF = px.index[-1].strftime("%Y-%m-%d")
print("prices:", px.shape, "as of", REF, file=sys.stderr)

# ---------- drift dataset ----------
held = [t for t in HOLD if t in px.columns and not px[t].isna().all()]
shares = pd.Series({t: HOLD[t][1] for t in held})
hp = px[held].loc[px.index >= pd.Timestamp(REBAL) - pd.Timedelta(days=7)]
mv = hp.mul(shares, axis=1)
w = mv.div(mv.sum(axis=1), axis=0) * 100
t0 = w.index[w.index <= pd.Timestamp(REBAL)][-1]
w0, wN = w.loc[t0], w.iloc[-1]
p0, pN = hp.loc[t0], hp.iloc[-1]
ret = (pN / p0 - 1) * 100
fund_ret = float(mv.sum(axis=1).iloc[-1] / mv.sum(axis=1).loc[t0] - 1) * 100
wk = w[(w.index.weekday == 4) | (w.index == w.index[-1])]
wk = wk[wk.index >= t0]

drift = {
    "asOf": REF, "rebalDate": t0.strftime("%Y-%m-%d"),
    "nextRebal": NEXT_REBAL.strftime("%Y-%m-%d"),
    "fundRet": round(fund_ret, 2),
    "dates": [d.strftime("%Y-%m-%d") for d in wk.index],
    "rows": [{
        "t": t, "n": HOLD[t][0], "sh": HOLD[t][1],
        "w0": round(float(w0[t]), 4), "w1": round(float(wN[t]), 4),
        "ret": round(float(ret[t]), 2), "px": round(float(pN[t]), 2),
        "s": [round(float(x), 4) for x in wk[t]],
    } for t in sorted(held, key=lambda t: -wN[t])],
}

# ---------- radar ----------
def ranks(ref):
    s = eng.momentum_scores(px, ref)
    return {t: int(r) for t, r in s["rank"].items()}

asof_idx = lambda d: px.index[px.index <= pd.Timestamp(d)][-1]
rk_now = ranks(REF)
rk_1w = ranks(asof_idx(pd.Timestamp(REF) - pd.Timedelta(days=7)))
rk_1m = ranks(asof_idx(pd.Timestamp(REF) - pd.Timedelta(days=30)))
rk_churn = ranks(t0)

sel, retained, adds = eng.select_with_buffer(rk_now, incumbents, N=100, buffer=0.20)
BUF_EDGE = 120
rows = []
for t, rank in sorted(rk_now.items(), key=lambda kv: kv[1]):
    is_held = t in incumbents
    if is_held:
        st = "held" if rank <= 100 else ("buffer" if rank <= BUF_EDGE else "atrisk")
    elif t in adds:
        st = "enter"
    elif rank <= 150:
        st = "challenger"
    else:
        continue
    dw = drift["rows"]
    rows.append({
        "t": t, "n": names.get(t, HOLD.get(t, (t,))[0]), "rank": rank,
        "d1w": rk_1w.get(t) and rk_1w[t] - rank,
        "d1m": rk_1m.get(t) and rk_1m[t] - rank,
        "rk0": rk_churn.get(t), "st": st,
        "w0": round(float(w0[t]), 4) if t in w0.index else None,
        "w1": round(float(wN[t]), 4) if t in wN.index else None,
    })
for t in missing:
    rows.append({"t": t, "n": HOLD[t][0], "rank": None, "d1w": None, "d1m": None,
                 "rk0": None, "st": "atrisk", "w0": None, "w1": None})

radar = {"ref": REF, "bufEdge": BUF_EDGE,
         "summary": {s: sum(1 for r in rows if r["st"] == s)
                     for s in ("held", "buffer", "atrisk", "enter", "challenger")},
         "rows": rows}
print("radar:", radar["summary"], file=sys.stderr)

# ---------- inject ----------
import re
html = open(ROOT / "template.html").read()
html = re.sub(r"/\*DATA_START\*/.*?/\*DATA_END\*/",
              "/*DATA_START*/\nconst DATA = " + json.dumps(drift) + ";\n/*DATA_END*/",
              html, flags=re.S)
html = re.sub(r"/\*RADAR_START\*/.*?/\*RADAR_END\*/",
              "/*RADAR_START*/\nconst RADAR = " + json.dumps(radar) + ";\n/*RADAR_END*/",
              html, flags=re.S)
assert '"asOf"' in html and '"bufEdge"' in html
open(ROOT / "index.html", "w").write(html)
print("index.html written", file=sys.stderr)
