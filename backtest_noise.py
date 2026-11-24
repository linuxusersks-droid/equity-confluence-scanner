"""
Intraday Momentum "Noise Area" backtest
(Zarattini, Aziz & Barbon 2024 logic adapted for NSE 5m Fyers data).
- Fyers se 5m candles leta hai (scanner.py ke get_fyers_token & send_telegram reuse karta hai)
- Pichle 14 dino ke har time-of-day move |Close/Open - 1| se dynamic Upper/Lower Noise Bands banata hai
- Nifty 50 Index par standalone test + 60 NSE stocks par 5 variants (A se E) compare karta hai
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
    ("A", "Noise Paper (k=1.0, 15m, Band+VWAP trail)",
     dict(k=1.0, check_mins=15, rvol=0.0, nifty=False, exit_mode="paper", tgt_r=None, top_n=5)),
    ("B", "Noise Wide (k=1.25, 15m, VWAP trail)",
     dict(k=1.25, check_mins=15, rvol=0.0, nifty=False, exit_mode="vwap", tgt_r=None, top_n=5)),
    ("C", "Noise + Vol + Nifty (k=1.0, RVOL>=1.5, 15m)",
     dict(k=1.0, check_mins=15, rvol=1.5, nifty=True, exit_mode="vwap", tgt_r=None, top_n=5)),
    ("D", "Noise Strict (k=1.25, RVOL>=1.5, Nifty, 30m)",
     dict(k=1.25, check_mins=30, rvol=1.5, nifty=True, exit_mode="vwap", tgt_r=None, top_n=5)),
    ("E", "Noise + 2.5R Target (k=1.0, RVOL>=1.5, Nifty)",
     dict(k=1.0, check_mins=15, rvol=1.5, nifty=True, exit_mode="fixed", tgt_r=2.5, top_n=5)),
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


def compute_noise_table(df):
    """
    Har time-of-day (smin) ke liye pichle 14 dino ka rolling mean |close/day_open - 1|
    calculate karta hai (strictly shifted by 1 day, zero lookahead bias).
    """
    day_open = df.groupby("date")["open"].transform("first")
    tmp = df[["date", "smin", "close"]].copy()
    tmp["abs_move"] = (tmp["close"] / day_open - 1.0).abs()
    piv = tmp.pivot_table(index="date", columns="smin", values="abs_move", aggfunc="last").sort_index()
    sigma = piv.shift(1).rolling(14, min_periods=7).mean()
    return sigma


def daily_meta(df):
    d = df.groupby("date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        vol=("volume", "sum"),
        n=("ts", "count"),
    ).sort_index()
    d["prev_close"] = d["close"].shift(1)
    d["avg14_vol"] = d["vol"].shift(1).rolling(14, min_periods=7).mean()
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - d["prev_close"]).abs(),
        (d["low"] - d["prev_close"]).abs(),
    ], axis=1).max(axis=1)
    d["atr14"] = tr.shift(1).rolling(14, min_periods=7).mean()
    return d


# ---------------------------------------------------------------- prep & signals
def build_day_cache(data, first_date, is_index=False):
    """Har (sym, date) ke liye arrays + noise sigma precompute karta hai."""
    cache = {}
    for sym, df in data.items():
        dm = daily_meta(df)
        sigma_tbl = compute_noise_table(df)
        for dt, g in df.groupby("date"):
            if dt < first_date or dt not in dm.index or dt not in sigma_tbl.index:
                continue
            row = dm.loc[dt]
            if pd.isna(row["prev_close"]) or pd.isna(row["atr14"]) or row["atr14"] <= 0 or len(g) < 60:
                continue
            if not is_index:
                if pd.isna(row["avg14_vol"]) or row["avg14_vol"] <= 0 or row["prev_close"] <= 100:
                    continue
            g = g.reset_index(drop=True)
            sig_row = sigma_tbl.loc[dt]
            sig_arr = np.array([sig_row.get(int(sm), np.nan) for sm in g["smin"]], dtype=float)
            if np.isnan(sig_arr).all():
                continue
            arr = {k: g[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "cumvol", "vwap")}
            arr["smin"] = g["smin"].to_numpy(dtype=int)
            arr["sigma"] = sig_arr
            arr["day_open"] = float(row["open"])
            arr["prev_close"] = float(row["prev_close"])
            arr["avg14_vol"] = float(row["avg14_vol"]) if not pd.isna(row["avg14_vol"]) else 0.0
            arr["atr14"] = float(row["atr14"])
            cache[(sym, dt)] = arr
    return cache


def bucket_of(scan_min):
    if scan_min < 630:
        return "09:30-10:30"
    if scan_min < 720:
        return "10:30-12:00"
    return "12:00-14:15"


def simulate_trade(sym, dt, arr, idx, side, p, is_index=False):
    k_mult = p["k"]
    step = p["check_mins"]
    exit_mode = p["exit_mode"]
    tgt_r = p["tgt_r"]

    entry = float(arr["close"][idx])
    ref_up = max(arr["day_open"], arr["prev_close"])
    ref_dn = min(arr["day_open"], arr["prev_close"])
    ub0 = ref_up * (1.0 + k_mult * arr["sigma"][idx])
    lb0 = ref_dn * (1.0 - k_mult * arr["sigma"][idx])

    vw0 = arr["vwap"][idx]
    has_vwap = (not is_index) and (not np.isnan(vw0)) and (vw0 > 0)

    # Risk (1R) = hard stop distance: VWAP ya 0.35*ATR14, floor 0.5% (index 0.35%)
    floor_pct = 0.0035 if is_index else 0.005
    if has_vwap and ((side == 1 and vw0 < entry) or (side == -1 and vw0 > entry)):
        raw_risk = abs(entry - vw0)
    else:
        raw_risk = 0.35 * arr["atr14"]
    risk = max(raw_risk, entry * floor_pct, 0.5)
    max_pct = 0.012 if is_index else 0.025
    risk = min(risk, entry * max_pct)

    hard_sl = entry - side * risk
    tgt = (entry + side * risk * tgt_r) if tgt_r else None

    exit_px, reason = None, "EOD"
    last_close = entry
    n = len(arr["close"])

    for j in range(idx + 1, n):
        sm = int(arr["smin"][j])
        if sm > 910:  # 15:10 candle close = 15:15 square-off
            break
        hi = float(arr["high"][j])
        lo = float(arr["low"][j])
        cl = float(arr["close"][j])
        vw = float(arr["vwap"][j])
        sig_j = arr["sigma"][j]

        # 1. Hard SL / Target check (har 5m candle par intrabar)
        if side == 1:
            if lo <= hard_sl:
                exit_px, reason = hard_sl, "SL"
                break
            if tgt is not None and hi >= tgt:
                exit_px, reason = tgt, "TGT"
                break
        else:
            if hi >= hard_sl:
                exit_px, reason = hard_sl, "SL"
                break
            if tgt is not None and lo <= tgt:
                exit_px, reason = tgt, "TGT"
                break

        # 2. Trailing close-based exit checkpoint bars par (whipsaw kam karne ke liye)
        bar_end = sm + 5
        if exit_mode in ("paper", "vwap") and ((bar_end - 555) % step == 0):
            ub_j = ref_up * (1.0 + k_mult * sig_j) if not np.isnan(sig_j) else ub0
            lb_j = ref_dn * (1.0 - k_mult * sig_j) if not np.isnan(sig_j) else lb0
            vw_ok = (not is_index) and (not np.isnan(vw)) and (vw > 0)

            if side == 1:
                if exit_mode == "paper":
                    trail = max(vw, ub_j) if vw_ok else ub_j
                else:
                    trail = vw if vw_ok else ub_j
                if cl < trail:
                    exit_px, reason = cl, "TRAIL"
                    break
            else:
                if exit_mode == "paper":
                    trail = min(vw, lb_j) if vw_ok else lb_j
                else:
                    trail = vw if vw_ok else lb_j
                if cl > trail:
                    exit_px, reason = cl, "TRAIL"
                    break

        last_close = cl

    if exit_px is None:
        exit_px = last_close

    r_gross = (exit_px - entry) * side / risk
    r_net = r_gross - (entry * COST_PCT / 100.0) / risk
    scan_min = int(arr["smin"][idx]) + 5
    return dict(
        date=str(dt),
        time=f"{scan_min // 60:02d}:{scan_min % 60:02d}",
        sym=sym,
        side="LONG" if side == 1 else "SHORT",
        entry=round(entry, 2),
        sl=round(hard_sl, 2),
        target=round(tgt, 2) if tgt is not None else "",
        exit=round(exit_px, 2),
        reason=reason,
        R=round(r_net, 3),
        bucket=bucket_of(scan_min),
    )


def run_variant(cache, nifty_map, p, is_index=False):
    by_slot = defaultdict(list)
    k_mult = p["k"]
    step = p["check_mins"]

    for (sym, dt), arr in cache.items():
        ref_up = max(arr["day_open"], arr["prev_close"])
        ref_dn = min(arr["day_open"], arr["prev_close"])
        n = len(arr["close"])
        for i in range(n):
            scan_min = int(arr["smin"][i]) + 5
            if scan_min < 570 or scan_min > 855:  # 09:30 se 14:15 tak
                continue
            if (scan_min - 555) % step != 0:
                continue
            sig_i = arr["sigma"][i]
            if np.isnan(sig_i) or sig_i <= 0:
                continue
            cl = float(arr["close"][i])
            ub = ref_up * (1.0 + k_mult * sig_i)
            lb = ref_dn * (1.0 - k_mult * sig_i)

            if cl > ub:
                side = 1
                strength = (cl - ub) / max(ub, 1.0)
            elif cl < lb:
                side = -1
                strength = (lb - cl) / max(lb, 1.0)
            else:
                continue

            proj = 1.0
            if not is_index:
                elapsed = max(min((scan_min - 555) / 375.0, 1.0), 0.04)
                proj = float(arr["cumvol"][i]) / arr["avg14_vol"] / elapsed
                if proj < p["rvol"]:
                    continue

            if p["nifty"] and not is_index:
                n_chg = nifty_map.get((dt, int(arr["smin"][i])), 0.0)
                if side == 1 and n_chg <= -0.2:
                    continue
                if side == -1 and n_chg >= 0.2:
                    continue

            by_slot[(dt, scan_min)].append(dict(
                sym=sym, dt=dt, i=i, side=side, score=proj if not is_index else strength
            ))

    trades = []
    taken_sym = defaultdict(set)
    day_count = defaultdict(int)

    for (dt, sm) in sorted(by_slot):
        cands = [c for c in by_slot[(dt, sm)] if c["sym"] not in taken_sym[dt]]
        cands.sort(key=lambda x: -x["score"])
        for c in cands:
            if day_count[dt] >= p["top_n"]:
                break
            taken_sym[dt].add(c["sym"])
            day_count[dt] += 1
            trades.append(
                simulate_trade(
                    c["sym"], dt, cache[(c["sym"], dt)], c["i"],
                    c["side"], p, is_index=is_index,
                )
            )
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
    lines = [f"NOISE AREA MOMENTUM BACKTEST {period_txt} | {n_syms} stocks | cost {COST_PCT}%", ""]
    lines.append("=== 60 NSE STOCKS ===")
    for key, name, _ in VARIANTS:
        lines.append(f"{key} {name}: {fmt(stats(results[key]))}")

    if nifty_res:
        lines += ["", "=== NIFTY 50 INDEX (STANDALONE) ==="]
        for label, tr in nifty_res:
            lines.append(f"{label}: {fmt(stats(tr))}")

    best_key = max(VARIANTS, key=lambda v: stats(results[v[0]])["tot"])[0]
    show_keys = ["A"] if best_key == "A" else ["A", best_key]
    for key in show_keys:
        name = dict((v[0], v[1]) for v in VARIANTS)[key]
        tr = results[key]
        lines += ["", f"{key} {name} - time & side:"]
        for b in ("09:30-10:30", "10:30-12:00", "12:00-14:15"):
            lines.append(f" {b}: {fmt(stats([t for t in tr if t['bucket'] == b]))}")
        for sd_lbl in ("LONG", "SHORT"):
            lines.append(f" {sd_lbl}: {fmt(stats([t for t in tr if t['side'] == sd_lbl]))}")

    sd = str(split_date)
    lines += ["", f"SPLIT: H1 = {sd} se pehle, H2 = {sd} ya uske baad"]
    for key, name, _ in VARIANTS:
        tr = results[key]
        lines.append(f"{key} {name}")
        lines.append(f" H1: {fmt(stats([t for t in tr if t['date'] < sd]))}")
        lines.append(f" H2: {fmt(stats([t for t in tr if t['date'] >= sd]))}")

    if nifty_res:
        for label, tr in nifty_res:
            lines.append(f"NIFTY {label}")
            lines.append(f" H1: {fmt(stats([t for t in tr if t['date'] < sd]))}")
            lines.append(f" H2: {fmt(stats([t for t in tr if t['date'] >= sd]))}")

    monthly = defaultdict(float)
    for t in results[best_key]:
        monthly[t["date"][:7]] += t["R"]
    lines += ["", f"Best ({best_key}) mahine-wise R:"]
    lines.append(" ".join(f"{m[2:]}:{v:+.1f}" for m, v in sorted(monthly.items())))
    lines += ["", "R = risk ke multiple. DD = max drawdown (R). PF = profit factor."]
    return "\n".join(lines)


# ---------------------------------------------------------------- main
def main():
    from fyers_apiv3 import fyersModel
    from scanner import get_fyers_token, send_telegram

    now = datetime.now(IST)
    start_dt = now - timedelta(days=BACKTEST_DAYS + WARMUP_DAYS)
    first_date = (now - timedelta(days=BACKTEST_DAYS)).date()

    app_id = os.environ["FYERS_APP_ID"].strip()
    token = get_fyers_token(now.strftime("%Y-%m-%d"))
    fyers = fyersModel.FyersModel(client_id=app_id, token=token, log_path="")

    if os.getenv("SYMBOLS"):
        symbols = [s.strip().upper() for s in os.getenv("SYMBOLS").split(",") if s.strip()]
    elif os.path.exists("symbols.txt"):
        symbols = [s.strip().upper() for s in open("symbols.txt") if s.strip()]
    else:
        symbols = DEFAULT_SYMBOLS

    print("Nifty fetch...")
    ndf = fetch_5m(fyers, "NSE:NIFTY50-INDEX", start_dt, now)
    nifty_map = build_nifty_map(ndf)
    if not nifty_map:
        print("WARNING: Nifty data nahi mila, Nifty filter kaam nahi karega")

    data = {}
    for k, s in enumerate(symbols, 1):
        print(f"[{k}/{len(symbols)}] {s}")
        df = fetch_5m(fyers, f"NSE:{s}-EQ", start_dt, now)
        if df is not None and not df.empty:
            data[s] = df
    if not data:
        raise SystemExit("Koi data nahi mila")

    cache = build_day_cache(data, first_date, is_index=False)
    print(f"Prepared stock-days: {len(cache)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    results = {}
    for key, name, p in VARIANTS:
        tr = run_variant(cache, nifty_map, p, is_index=False)
        results[key] = tr
        pd.DataFrame(tr).to_csv(f"{OUT_DIR}/trades_{key}.csv", index=False)

    nifty_res = []
    if ndf is not None and not ndf.empty:
        ncache = build_day_cache({"NIFTY50": ndf}, first_date, is_index=True)
        for label, np_cfg in [
            ("N1 (k=1.0, 15m, Band trail)", dict(k=1.0, check_mins=15, rvol=0.0, nifty=False, exit_mode="paper", tgt_r=None, top_n=1)),
            ("N2 (k=1.0, 30m, Band trail)", dict(k=1.0, check_mins=30, rvol=0.0, nifty=False, exit_mode="paper", tgt_r=None, top_n=1)),
        ]:
            ntr = run_variant(ncache, {}, np_cfg, is_index=True)
            nifty_res.append((label, ntr))
            tag = label.split()[0]
            pd.DataFrame(ntr).to_csv(f"{OUT_DIR}/trades_{tag}.csv", index=False)

    sd_env = os.getenv("SPLIT_DATE", "").strip()
    if sd_env:
        split_date = datetime.strptime(sd_env, "%Y-%m-%d").date()
    else:
        split_date = first_date + (now.date() - first_date) / 2
    report = build_report(results, nifty_res, f"{first_date} se {now.date()}", len(data), split_date)
    print(report)
    with open(f"{OUT_DIR}/summary.txt", "w") as f:
        f.write(report)

    chunk = ""
    for ln in report.split("\n"):
        if len(chunk) + len(ln) + 1 > 3800:
            send_telegram(html.escape(chunk))
            chunk = ""
        chunk += ln + "\n"
    if chunk.strip():
        send_telegram(html.escape(chunk))


if __name__ == "__main__":
    main()
