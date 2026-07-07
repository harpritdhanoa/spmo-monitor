#!/usr/bin/env python3
"""
Momentum index replication & walk-forward validation engine.
Built and validated on Invesco S&P 500 Momentum ETF (SPMO). See ../SPMO.md.

Parameterised so the SAME engine runs other indices/recipes:
  - SPMO  : BLEND_6M = 0.0  (12-month only)        <-- validated
  - MTUM  : BLEND_6M = 0.5  (MSCI 6m+12m blend) + an MSCI-USA universe adapter

Requires: pandas, numpy, yfinance, lxml.  Network: Yahoo Finance, Wikipedia, SEC EDGAR.

This is the consolidated reference implementation. Adapt CONFIG and the universe/
holdings adapters for a new market (see ../../../reference/data-sources.md).
"""
import urllib.request, io, re, html, time
from datetime import datetime
import numpy as np, pandas as pd

# ----------------------- CONFIG -----------------------
REFERENCE_DATE   = "2026-02-28"   # last business day of Feb/Aug for SPMO
LOOKBACK_M, LAG_M = 12, 1         # 12-1 momentum
BLEND_6M         = 0.0            # 0.0 = 12m only (SPMO); 0.5 = MSCI blend (MTUM)
VOL_YEARS        = 3
WINSOR           = 3.0
TARGET_N         = 100            # or set to the actual fund book size
BUFFER           = 0.20           # 20% count buffer
SINGLE_CAP       = 0.10           # 10% issuer-level cap
EDGAR_SERIES     = "S000050154"   # SPMO; None to skip validation
EDGAR_CIK        = "1378872"
UA = {"User-Agent": "Research Contact research@example.com"}

def _get(url, ua=UA, timeout=40):
    return urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=timeout).read()

# --------------- UNIVERSE ADAPTER (S&P 500) ---------------
def sp500_point_in_time(ref_date):
    """Reconstruct S&P 500 membership as of ref_date by reversing Wikipedia changes."""
    raw = _get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
               ua={"User-Agent": "Mozilla/5.0"})
    t = pd.read_html(io.BytesIO(raw))
    cur = t[0][["Symbol", "Security", "GICS Sector"]]
    cur.columns = ["Symbol", "Name", "Sector"]
    cur["Symbol"] = cur["Symbol"].str.replace(".", "-", regex=False)
    chg = t[1]; chg.columns = ["_".join(map(str, c)) if isinstance(c, tuple) else c for c in chg.columns]
    dcol = [c for c in chg.columns if "Date" in c][0]
    acol = [c for c in chg.columns if "Added" in c and "Ticker" in c][0]
    rcol = [c for c in chg.columns if "Removed" in c and "Ticker" in c][0]
    def pd_(s):
        for f in ("%B %d, %Y", "%b %d, %Y"):
            try: return datetime.strptime(str(s).strip(), f)
            except: pass
        return None
    cut = datetime.strptime(ref_date, "%Y-%m-%d")
    members = set(cur["Symbol"]); meta = {r.Symbol: (r.Name, r.Sector) for r in cur.itertuples()}
    for r in chg.itertuples(index=False):
        d = pd_(getattr(r, dcol.replace(" ", "_")) if hasattr(r, dcol.replace(" ", "_")) else r[chg.columns.get_loc(dcol)])
        d = pd_(r[chg.columns.get_loc(dcol)]) if d is None else d
        if d is None or d <= cut: continue
        add = str(r[chg.columns.get_loc(acol)]).replace(".", "-").strip()
        rem = str(r[chg.columns.get_loc(rcol)]).replace(".", "-").strip()
        if add and add != "nan": members.discard(add)
        if rem and rem != "nan": members.add(rem)
    return pd.DataFrame([{"Symbol": m, "Name": meta.get(m, (m, "?"))[0],
                          "Sector": meta.get(m, (m, "?"))[1]} for m in sorted(members)])

# --------------- PRICES ---------------
def download_prices(tickers, start, end):
    import yfinance as yf
    out = []
    for i in range(0, len(tickers), 170):
        d = yf.download(tickers[i:i+170], start=start, end=end, auto_adjust=True,
                        progress=False, threads=True)
        out.append(d["Close"] if isinstance(d.columns, pd.MultiIndex) else d)
    px = pd.concat(out, axis=1); px.index = pd.to_datetime(px.index)
    return px.sort_index()

# --------------- SCORING ---------------
def momentum_scores(px, ref_date, lookback_m=LOOKBACK_M, lag_m=LAG_M,
                    blend_6m=BLEND_6M, vol_years=VOL_YEARS, winsor=WINSOR):
    asof = lambda d: px.index[px.index <= pd.Timestamp(d)][-1]
    ref = asof(ref_date)
    end_m = asof(pd.Timestamp(ref) - pd.DateOffset(months=lag_m))
    s12   = asof(pd.Timestamp(end_m) - pd.DateOffset(months=lookback_m))
    s6    = asof(pd.Timestamp(end_m) - pd.DateOffset(months=6))
    raw12 = px.loc[end_m] / px.loc[s12] - 1
    wk = px.loc[:ref].resample("W-FRI").last()
    wk = wk.loc[wk.index >= ref - pd.DateOffset(years=vol_years)]
    vol = wk.pct_change(fill_method=None).std() * np.sqrt(52)
    z = lambda s: (s - s.mean()) / s.std()
    df = pd.DataFrame({"ram12": raw12 / vol}).dropna()
    df = df[np.isfinite(df["ram12"])]
    if blend_6m > 0:
        raw6 = px.loc[end_m] / px.loc[s6] - 1
        df["ram6"] = (raw6 / vol).reindex(df.index)
        df = df.dropna()
        zb = (1 - blend_6m) * z(df["ram12"]) + blend_6m * z(df["ram6"])
    else:
        zb = z(df["ram12"])
    df["z"] = zb.clip(-winsor, winsor)
    df["score"] = np.where(df["z"] >= 0, 1 + df["z"], 1 / (1 - df["z"]))
    return df.sort_values("score", ascending=False).assign(rank=lambda x: range(1, len(x) + 1))

# --------------- SELECTION (buffer) ---------------
def select_with_buffer(rank_by_ticker, incumbents, N=TARGET_N, buffer=BUFFER):
    buf = int(round((1 + buffer) * N))
    retained = sorted([t for t in incumbents if rank_by_ticker.get(t, 10**9) <= buf],
                      key=lambda t: rank_by_ticker[t])
    newcomers = [t for t in sorted(rank_by_ticker, key=rank_by_ticker.get) if t not in incumbents]
    adds = newcomers[:max(0, N - len(retained))]
    return set(retained) | set(adds), set(retained), set(adds)

# --------------- WEIGHTING (issuer cap) ---------------
DUAL = {"GOOG": "GOOGL", "FOX": "FOXA", "NWS": "NWSA"}
def issuer(t): return DUAL.get(t, t)

def capped_weights(raw_mktcap_x_score, cap=SINGLE_CAP):
    """raw_mktcap_x_score: Series indexed by ticker. Returns issuer-capped, normalised weights."""
    w = (raw_mktcap_x_score / raw_mktcap_x_score.sum()).astype(float)
    iss = pd.Series({t: issuer(t) for t in w.index})
    W = w.groupby(iss).sum()
    for _ in range(300):
        over = W > cap + 1e-12
        if not over.any(): break
        exc = (W[over] - cap).sum(); W[over] = cap
        und = ~over; W[und] += exc * W[und] / W[und].sum()
    # split issuer weight back to tickers proportionally
    return pd.Series({t: w[t] * (W[issuer(t)] / w.groupby(iss).sum()[issuer(t)]) for t in w.index})

# --------------- EDGAR validation ---------------
def edgar_nport_holdings(series_id=EDGAR_SERIES, want_period=None):
    """Return DataFrame(name,cusip,pct,repPdDate) for the fund's N-PORT closest to want_period."""
    atom = _get(f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={series_id}"
                f"&type=NPORT-P&count=40&output=atom").decode("utf-8", "ignore")
    accs = re.findall(r"<accession-number>(.*?)<", atom)
    best = None
    for acc in accs:
        fold = acc.replace("-", "")
        try:
            xml = _get(f"https://www.sec.gov/Archives/edgar/data/{EDGAR_CIK}/{fold}/primary_doc.xml").decode("utf-8", "ignore")
        except Exception:
            continue
        rep = re.search(r"<repPdDate>(.*?)</repPdDate>", xml)
        rep = rep.group(1) if rep else "?"
        rows = []
        for blk in re.findall(r"<invstOrSec>(.*?)</invstOrSec>", xml, re.S):
            ac = re.search(r"<assetCat>(.*?)</assetCat>", blk)
            if ac and ac.group(1) != "EC": continue
            g = lambda t: (re.search(rf"<{t}>(.*?)</{t}>", blk, re.S) or [None, None])[1]
            rows.append({"name": html.unescape(g("name") or ""), "cusip": g("cusip"),
                         "pct": float(g("pctVal") or "nan")})
        df = pd.DataFrame(rows); df.attrs["repPdDate"] = rep
        if want_period is None or rep == want_period:
            return df
        best = df
    return best

if __name__ == "__main__":
    print("Reference date:", REFERENCE_DATE, "| BLEND_6M:", BLEND_6M)
    uni = sp500_point_in_time(REFERENCE_DATE)
    print("Point-in-time universe:", len(uni))
    px = download_prices(uni["Symbol"].tolist(), "2023-01-01",
                         (pd.Timestamp(REFERENCE_DATE) + pd.Timedelta(days=2)).strftime("%Y-%m-%d"))
    scored = momentum_scores(px, REFERENCE_DATE)
    print("Top 10:", list(scored.head(10).index))
    # selection/weights require incumbents (prior N-PORT) + shares; see SPMO.md for the full
    # walk-forward harness. This entrypoint demonstrates universe -> prices -> scores.
