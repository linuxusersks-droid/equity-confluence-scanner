"""
ICT / SMC Liquidity Sweep + MSS + FVG Backtest
(PDH/PDL & Initial Balance 09:15-09:30 Liquidity Sweeps adapted for NSE 5m Fyers data).
- Fyers se 5m candles leta hai (scanner.py ke get_fyers_token & send_telegram reuse karta hai)
- 60 NSE stocks par 5 mechanical ICT variants (A se E) + Nifty 50 standalone (N1, N2) compare karta hai
- Strict zero-lookahead rules & conservative intrabar SL-first execution
- Summary Telegram par bhejta hai, trades CSV backtest_out/ me save karta hai
"""
import html
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = os.getenv("OUT_DIR", "backtest_out") or "backtest_out"
COLS = ["ts", "open", "high", "low", "close", "volume"]

BACKTEST_DAYS = int(os.getenv("BACKTEST_DAYS", "180") or "180")
WARMUP_DAYS = 25
COST_PCT = float(os.getenv("COST_PCT", "0.05") or "0.05")  # round-trip cost %

DEFAULT_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL",
    "ITC", "LT", "KOTAKBANK", "AXISBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI",
    "SUNPHARMA", "TATASTEEL", "NTPC", "POWERGRID", "ONGC", "ULTRACEMCO",
    "ASIANPAINT", "TITAN", "M&M", "HCLTECH", "WIPRO", "TECHM", "ADANIENT",
    "ADANIPORTS", "JSWSTEEL", "COALINDIA", "BAJAJFINSV", "NESTLEIND", "DRREDDY",
    "CIPLA", "EICHERMOT", "HEROMOTOCO", "GRASIM", "HINDALCO", "BPCL",
    "INDUSINDBK", "SBILIFE", "HDFCLIFE", "TRENT", "BEL", "DLF", "HAL", "VEDL",
    "PNB", "BANKBARODA", "IRCTC", "ECLERX", "LINDEINDIA", "AMBUJACEM",
    "PERSISTENT", "COFORGE", "POLYCAB", "DIXON", "LUPIN", "TVSMOTOR", "CHOLAFIN",
]

VARIANTS = [
    ("A", "ICT Classic (Sweep+MSS+FVG retest, 2.5R)",
     dict(mode="fvg_retest", end_min=720, rvol=0.0, nifty=False, tgt_r=2.5, be_r=None, top_n=5)),
    ("B", "ICT Killzone 3R (09:30-11:15, FVG retest)",
     dict(mode="fvg_retest", end_min=675, rvol=0.0, nifty=False, tgt_r=3.0, be_r=None, top_n=5)),
    ("C", "ICT Filtered (RVOL>=1.2 + Nifty, FVG 2.5R)",
     dict(mode="fvg_retest", end_min=720, rvol=1.2, nifty=True, tgt_r=2.5, be_r=None, top_n=5)),
    ("D", "Sweep Wick Reversal (09:30-11:15, 2R+BE@1R)",
     dict(mode="sweep_wick", end_min=675, rvol=0.0, nifty=False, tgt_r=2.0, be_r=1.0, top_n=5)),
    ("E", "ICT MSS Direct (No retest wait, 2.5R)",
     dict(mode="mss_direct", end_min=690, rvol=1.2, nifty=True, tgt_r=2.5, be_r=None, top_n=5)),
]


# ---------------------------------------------------------------- data
def prepare(df):
    df = df.drop_duplicates("ts").sort_values("ts").copy()
    t = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df["date"] = t.dt.date
    df["smin"] = t.dt.hour * 60 + t.dt.minute
    df = df[(df["smin"] >= 555) & (df["smin"] <= 925)].reset_index(drop=True)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["pv"] = tp * df["volume"]
    df["cumvol"] = df.groupby("date")["volume"].cumsum()
    df["cumpv"] = df.groupby("date")["pv"].cumsum()
    df["vwap"] = df["cumpv"] / df["cumvol"].replace(0, np.nan)
    return df


def fetch_5m(fyers, fy_sym, start_dt, end_dt):
    frames = []
    cur = start_dt
    while cur < end_dt:
        nxt = min(cur + timedelta(days=90), end_dt)
        req = {
            "symbol": fy_sym,
            "resolution": "5",
            "date_format": "0",
            "range_from": str(int(cur.timestamp())),
            "range_to": str(int(nxt.timestamp())),
            "cont_flag": "1",
        }
        resp = {}
        for _ in range(3):
            try:
                resp = fyers.history(data=req)
            except Exception as e:
                resp = {"s": "error", "message": str(e)}
            if resp.get("s") == "ok":
                break
            time.sleep(1.5)
        time.sleep(0.3)
        if resp.get("s") == "ok" and resp.get("candles"):
            frames.append(pd.DataFrame(resp["candles"], columns=COLS))
        else:
            print(f"  {fy_sym}: chunk fail/empty {resp.get('s')} {resp.get('message', '')}")
        cur = nxt
    if not frames:
        return None
    return prepare(pd.concat(frames))


def build_nifty_map(ndf):
    if ndf is None or ndf.empty:
        return {}
    last_close = ndf.groupby("date")["close"].last()
    prev = last_close.shift(1)
    m = {}
    for dt, smin, c in zip(ndf["date"], ndf["smin"], ndf["close"]):
        p = prev.get(dt)
        if p is not None and not pd.isna(p) and p > 0:
            m[(dt, int(smin))] = (c - p) / p * 100
    return m


def daily_levels(df):
    d = df.groupby("date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        vol=("volume", "sum"),
        n=("ts", "count"),
    ).sort_index()
    d["pdh"] = d["high"].shift(1)
    d["pdl"] = d["low"].shift(1)
    d["prev_close"] = d["close"].shift(1)
    d["avg14_vol"] = d["vol"].shift(1).rolling(14, min_periods=5).mean()
    return d


def build_day_cache(data, first_date, is_index=False):
    cache = {}
    for sym, df in data.items():
        dl = daily_levels(df)
        for dt, g in df.groupby("date"):
            if dt < first_date or dt not in dl.index:
                continue
            row = dl.loc[dt]
            if pd.isna(row["pdh"]) or pd.isna(row["pdl"]) or pd.isna(row["prev_close"]) or len(g) < 60:
                continue
            if not is_index:
                if pd.isna(row["avg14_vol"]) or row["avg14_vol"] <= 0 or row["prev_close"] <= 100:
                    continue
            g = g.reset_index(drop=True)
            ib = g[(g["smin"] >= 555) & (g["smin"] <= 565)]  # 09:15, 09:20, 09:25 -> IB complete at 09:30
            if len(ib) < 3:
                continue
            ibh = float(ib["high"].max())
            ibl = float(ib["low"].min())
            arr = {k: g[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume", "cumvol", "vwap")}
            arr["smin"] = g["smin"].to_numpy(dtype=int)
            arr["pdh"] = float(row["pdh"])
            arr["pdl"] = float(row["pdl"])
            arr["prev_close"] = float(row["prev_close"])
            arr["day_open"] = float(row["open"])
            arr["ibh"] = ibh
            arr["ibl"] = ibl
            arr["avg14_vol"] = float(row["avg14_vol"]) if not pd.isna(row["avg14_vol"]) else 0.0
            cache[(sym, dt)] = arr
    return cache


# ---------------------------------------------------------------- ICT detection
def bucket_of(scan_min):
    if scan_min < 630:
        return "09:30-10:30"
    if scan_min < 690:
        return "10:30-11:30"
    return "11:30-12:00+"


def detect_signals_for_day(sym, dt, arr, p, nifty_map, is_index=False):
    """
    Har (sym, date) par pehla valid ICT setup detect karta hai (strictly causal).
    """
    o = arr["open"]
    h = arr["high"]
    l = arr["low"]
    c = arr["close"]
    smin = arr["smin"]
    n = len(c)

    mode = p["mode"]
    end_min = p["end_min"]

    # Index mean-reversion ke liye gap & IB range filters
    gap_pct = abs(arr["day_open"] - arr["prev_close"]) / arr["prev_close"] * 100.0
    if is_index and mode == "sweep_wick" and gap_pct > 1.0:
        return None
    ib_rng_pct = (arr["ibh"] - arr["ibl"]) / arr["day_open"] * 100.0
    if is_index and mode == "sweep_wick" and ib_rng_pct > 0.75:
        return None

    min_sweep = 0.0005  # 0.05% minimum liquidity poke
    max_sweep = 0.008 if is_index else 0.015
    min_fvg = 0.0004 if is_index else 0.0006

    # ---- Mode 1: Direct Liquidity Sweep + Rejection Wick
    if mode == "sweep_wick":
        for i in range(3, n - 1):
            scan_min = int(smin[i]) + 5
            if scan_min < 575 or scan_min > end_min:
                continue
            rng = h[i] - l[i]
            if rng <= 0:
                continue
            upper_wick = (h[i] - max(o[i], c[i])) / rng
            lower_wick = (min(o[i], c[i]) - l[i]) / rng

            side = 0
            sweep_ext = None
            lvl_tag = ""
            for lvl, tag in ((arr["ibh"], "IBH"), (arr["pdh"], "PDH")):
                depth = (h[i] - lvl) / lvl
                if min_sweep <= depth <= max_sweep and c[i] < lvl and upper_wick >= 0.25:
                    if c[i - 1] <= lvl * 1.001:
                        side = -1
                        sweep_ext = h[i]
                        lvl_tag = tag
                        break

            if side == 0:
                for lvl, tag in ((arr["ibl"], "IBL"), (arr["pdl"], "PDL")):
                    depth = (lvl - l[i]) / lvl
                    if min_sweep <= depth <= max_sweep and c[i] > lvl and lower_wick >= 0.25:
                        if c[i - 1] >= lvl * 0.999:
                            side = 1
                            sweep_ext = l[i]
                            lvl_tag = tag
                            break

            if side == 0:
                continue
            if not _pass_filters(sym, dt, arr, i, side, p, nifty_map, is_index):
                continue

            entry = float(c[i])
            buf = entry * 0.0005
            raw_risk = (entry - (sweep_ext - buf)) if side == 1 else ((sweep_ext + buf) - entry)
            return dict(
                sym=sym, dt=dt, entry_idx=i, fill_on_open_bar=False,
                scan_min=scan_min, side=side, entry=entry,
                raw_risk=raw_risk, score=_score(arr, i, is_index),
                tag=f"SWEEP_{lvl_tag}",
            )
        return None

    # ---- Mode 2 & 3: ICT Liquidity Sweep -> MSS + FVG (Retest ya Direct)
    active_sweep = None
    pending_fvg = None

    for i in range(3, n - 1):
        scan_min = int(smin[i]) + 5
        if scan_min < 575:
            continue

        # 1. Check pending FVG limit order on candle i (strictly causal)
        if pending_fvg is not None:
            if i - pending_fvg["fvg_idx"] > 8 or scan_min > end_min + 30:
                pending_fvg = None
            else:
                side = pending_fvg["side"]
                fvg_lvl = pending_fvg["fvg_entry"]
                sweep_ext = pending_fvg["sweep_ext"]
                if side == -1:
                    if o[i] >= sweep_ext:
                        pending_fvg = None
                    elif h[i] >= fvg_lvl:
                        entry = max(float(o[i]), fvg_lvl)
                        buf = entry * 0.0005
                        raw_risk = (sweep_ext + buf) - entry
                        return dict(
                            sym=sym, dt=dt, entry_idx=i, fill_on_open_bar=True,
                            scan_min=scan_min, side=side, entry=entry,
                            raw_risk=raw_risk, score=pending_fvg["score"],
                            tag=f"FVG_{pending_fvg['tag']}",
                        )
                else:
                    if o[i] <= sweep_ext:
                        pending_fvg = None
                    elif l[i] <= fvg_lvl:
                        entry = min(float(o[i]), fvg_lvl)
                        buf = entry * 0.0005
                        raw_risk = entry - (sweep_ext - buf)
                        return dict(
                            sym=sym, dt=dt, entry_idx=i, fill_on_open_bar=True,
                            scan_min=scan_min, side=side, entry=entry,
                            raw_risk=raw_risk, score=pending_fvg["score"],
                            tag=f"FVG_{pending_fvg['tag']}",
                        )

        if scan_min > end_min:
            continue

        # 2. Check for new liquidity sweep on candle i
        for lvl, tag in ((arr["ibh"], "IBH"), (arr["pdh"], "PDH")):
            depth = (h[i] - lvl) / lvl
            if min_sweep <= depth <= max_sweep and l[i] < lvl:
                mss_lvl = float(l[max(0, i - 3):i + 1].min())
                active_sweep = dict(
                    side=-1, sweep_idx=i, sweep_ext=float(h[i]),
                    mss_lvl=mss_lvl, tag=tag,
                )
                break
        else:
            for lvl, tag in ((arr["ibl"], "IBL"), (arr["pdl"], "PDL")):
                depth = (lvl - l[i]) / lvl
                if min_sweep <= depth <= max_sweep and h[i] > lvl:
                    mss_lvl = float(h[max(0, i - 3):i + 1].max())
                    active_sweep = dict(
                        side=1, sweep_idx=i, sweep_ext=float(l[i]),
                        mss_lvl=mss_lvl, tag=tag,
                    )
                    break

        # 3. Look for MSS + FVG within 6 candles of sweep
        if active_sweep is not None and i > active_sweep["sweep_idx"]:
            if i - active_sweep["sweep_idx"] > 6:
                active_sweep = None
                continue

            side = active_sweep["side"]
            if side == -1:
                active_sweep["sweep_ext"] = max(active_sweep["sweep_ext"], float(h[i]))
            else:
                active_sweep["sweep_ext"] = min(active_sweep["sweep_ext"], float(l[i]))

            if i < 2:
                continue

            if side == -1:
                mss_ok = c[i] < active_sweep["mss_lvl"]
                fvg_gap = l[i - 2] - h[i]
                fvg_ok = (fvg_gap / c[i]) >= min_fvg
                fvg_entry = float(h[i])
            else:
                mss_ok = c[i] > active_sweep["mss_lvl"]
                fvg_gap = l[i] - h[i - 2]
                fvg_ok = (fvg_gap / c[i]) >= min_fvg
                fvg_entry = float(l[i])

            if mss_ok and fvg_ok:
                if not _pass_filters(sym, dt, arr, i, side, p, nifty_map, is_index):
                    active_sweep = None
                    continue

                if mode == "mss_direct":
                    entry = float(c[i])
                    buf = entry * 0.0005
                    sweep_ext = active_sweep["sweep_ext"]
                    raw_risk = (entry - (sweep_ext - buf)) if side == 1 else ((sweep_ext + buf) - entry)
                    return dict(
                        sym=sym, dt=dt, entry_idx=i, fill_on_open_bar=False,
                        scan_min=scan_min, side=side, entry=entry,
                        raw_risk=raw_risk, score=_score(arr, i, is_index),
                        tag=f"MSS_{active_sweep['tag']}",
                    )
                else:
                    pending_fvg = dict(
                        side=side, fvg_idx=i, fvg_entry=fvg_entry,
                        sweep_ext=active_sweep["sweep_ext"], tag=active_sweep["tag"],
                        score=_score(arr, i, is_index),
                    )
                    active_sweep = None

    return None


def _score(arr, i, is_index):
    if is_index or arr["avg14_vol"] <= 0:
        return 1.0
    scan_min = int(arr["smin"][i]) + 5
    elapsed = max(min((scan_min - 555) / 375.0, 1.0), 0.04)
    return float(arr["cumvol"][i]) / arr["avg14_vol"] / elapsed


def _pass_filters(sym, dt, arr, i, side, p, nifty_map, is_index):
    if not is_index and p["rvol"] > 0:
        if _score(arr, i, is_index) < p["rvol"]:
            return False
    if not is_index and p["nifty"]:
        n_chg = nifty_map.get((dt, int(arr["smin"][i])), 0.0)
        if side == 1 and n_chg <= -0.2:
            return False
        if side == -1 and n_chg >= 0.2:
            return False
    return True


# ---------------------------------------------------------------- simulate
def simulate(sig, arr, p, is_index=False):
    entry = sig["entry"]
    side = sig["side"]
    raw_risk = sig["raw_risk"]
    if raw_risk <= 0:
        return None

    min_pct = 0.0012 if is_index else 0.0035
    max_pct = 0.006 if is_index else 0.020
    if raw_risk > entry * max_pct:
        return None
    risk = max(raw_risk, entry * min_pct, 0.5)

    sl = entry - side * risk
    cur_sl = sl
    tgt = entry + side * risk * p["tgt_r"]
    be_trigger = (entry + side * risk * p["be_r"]) if p["be_r"] else None
    be_armed = False

    exit_px, reason = None, "EOD"
    n = len(arr["close"])
    idx = sig["entry_idx"]
    last_close = float(arr["close"][idx])

    # Conservative same-bar SL check for limit fills
    if sig["fill_on_open_bar"]:
        hi, lo = float(arr["high"][idx]), float(arr["low"][idx])
        if side == 1 and lo <= cur_sl:
            exit_px, reason = cur_sl, "SL"
        elif side == -1 and hi >= cur_sl:
            exit_px, reason = cur_sl, "SL"

    if exit_px is None:
        for j in range(idx + 1, n):
            if int(arr["smin"][j]) > 910:  # 15:15 square-off
                break
            hi = float(arr["high"][j])
            lo = float(arr["low"][j])
            cl = float(arr["close"][j])

            if side == 1:
                if lo <= cur_sl:
                    exit_px, reason = cur_sl, ("BE" if be_armed else "SL")
                    break
                if hi >= tgt:
                    exit_px, reason = tgt, "TGT"
                    break
                if be_trigger is not None and (not be_armed) and hi >= be_trigger:
                    cur_sl = entry
                    be_armed = True
            else:
                if hi >= cur_sl:
                    exit_px, reason = cur_sl, ("BE" if be_armed else "SL")
                    break
                if lo <= tgt:
                    exit_px, reason = tgt, "TGT"
                    break
                if be_trigger is not None and (not be_armed) and lo <= be_trigger:
                    cur_sl = entry
                    be_armed = True
            last_close = cl

    if exit_px is None:
        exit_px = last_close

    r_gross = (exit_px - entry) * side / risk
    r_net = r_gross - (entry * COST_PCT / 100.0) / risk
    sm = sig["scan_min"]
    return dict(
        date=str(sig["dt"]),
        time=f"{sm // 60:02d}:{sm % 60:02d}",
        sym=sig["sym"],
        side="LONG" if side == 1 else "SHORT",
        setup=sig["tag"],
        entry=round(entry, 2),
        sl=round(sl, 2),
        target=round(tgt, 2),
        exit=round(exit_px, 2),
        reason=reason,
        R=round(r_net, 3),
        bucket=bucket_of(sm),
    )


def run_variant(cache, nifty_map, p, is_index=False):
    by_day = defaultdict(list)
    for (sym, dt), arr in cache.items():
        sig = detect_signals_for_day(sym, dt, arr, p, nifty_map, is_index=is_index)
        if sig is not None:
            by_day[dt].append(sig)

    trades = []
    for dt in sorted(by_day):
        sigs = sorted(by_day[dt], key=lambda x: (x["scan_min"], -x["score"]))
        taken = 0
        for s in sigs:
            if taken >= p["top_n"]:
                break
            t = simulate(s, cache[(s["sym"], dt)], p, is_index=is_index)
            if t is not None:
                trades.append(t)
                taken += 1
    return trades


# ---------------------------------------------------------------- report
def stats(trades):
    n = len(trades)
    if n == 0:
        return dict(n=0, win=0.0, avg=0.0, tot=0.0, pf=0.0, dd=0.0)
    R = np.array([t["R"] for t in trades])
    wins, losses = R[R > 0], R[R <= 0]
    if losses.sum() < 0:
        pf = wins.sum() / abs(losses.sum())
    else:
        pf = float("inf") if wins.sum() > 0 else 0.0
    cum = np.cumsum(R)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    return dict(n=n, win=len(wins) / n * 100, avg=float(R.mean()), tot=float(R.sum()), pf=float(pf), dd=dd)


def fmt(s):
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return f"n={s['n']} win={s['win']:.0f}% avgR={s['avg']:+.2f} totR={s['tot']:+.1f} PF={pf} DD={s['dd']:.1f}R"


def build_report(results, nifty_res, period_txt, n_syms, split_date):
    lines = [f"ICT / SMC BACKTEST {period_txt} | {n_syms} stocks | cost {COST_PCT}%", ""]
    lines.append("=== 60 NSE STOCKS ===")
    for key, name, _ in VARIANTS:
        lines.append(f"{key} {name}: {fmt(stats(results[key]))}")

    if nifty_res:
        lines += ["", "=== NIFTY 50 INDEX (STANDALONE) ==="]
        for label, tr in nifty_res:
            lines.append(f"{label}: {fmt(stats(tr))}")
