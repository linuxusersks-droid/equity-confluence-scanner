"""
5m Opening Range Breakout (ORB) "Stocks in Play" backtest
(Zarattini, Barbon & Aziz 2024 logic adapted for NSE 5m Fyers data).
- Fyers se 5m candles leta hai (scanner.py ke get_fyers_token & send_telegram reuse karta hai)
- Daily first 5m candle (09:15-09:20) ke RVOL par top Stocks-in-Play select karta hai
- 5 filter/exit variants compare karta hai (A se E)
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
    ("A", "ORB Classic (RVOL>=1.5, OR-SL, EOD)",
     dict(rvol=1.5, top_n=5, nifty=False, sl_mode="or", tgt_r=None, vwap_exit=False)),
    ("B", "ORB ATR-SL (RVOL>=1.5, 0.25*ATR, EOD)",
     dict(rvol=1.5, top_n=5, nifty=False, sl_mode="atr", tgt_r=None, vwap_exit=False)),
    ("C", "ORB + 2.5R Target (RVOL>=1.5, OR-SL)",
     dict(rvol=1.5, top_n=5, nifty=False, sl_mode="or", tgt_r=2.5, vwap_exit=False)),
    ("D", "ORB Strict (RVOL>=2.0, Nifty, OR-SL, EOD)",
     dict(rvol=2.0, top_n=5, nifty=True, sl_mode="or", tgt_r=None, vwap_exit=False)),
    ("E", "ORB + VWAP Trail (RVOL>=1.5, Nifty, OR-SL)",
     dict(rvol=1.5, top_n=5, nifty=True, sl_mode="or", tgt_r=None, vwap_exit=True)),
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


def daily_orb_stats(df):
    first_bar = df[df["smin"] == 555].groupby("date")["volume"].first()
    d = df.groupby("date").agg(
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        vol=("volume", "sum"),
        n=("ts", "count"),
    ).sort_index()
    d["open5_vol"] = first_bar
    d["prev_close"] = d["close"].shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - d["prev_close"]).abs(),
        (d["low"] - d["prev_close"]).abs(),
    ], axis=1).max(axis=1)
    d["atr14"] = tr.shift(1).rolling(14, min_periods=7).mean()
    d["avg_open5_vol"] = d["open5_vol"].shift(1).rolling(14, min_periods=5).mean()
    return d


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


# ---------------------------------------------------------------- setups
def build_setups(data, nifty_map, first_date):
    """Har din 09:15-09:20 candle close par RVOL aur OR direction calculate karta hai."""
    setups, days = [], {}
    for sym, df in data.items():
        ds = daily_orb_stats(df)
        for dt, g in df.groupby("date"):
            if dt < first_date or dt not in ds.index:
                continue
            row = ds.loc[dt]
            if (pd.isna(row["prev_close"]) or pd.isna(row["atr14"])
                    or pd.isna(row["avg_open5_vol"]) or row["avg_open5_vol"] <= 0
                    or row["atr14"] <= 0 or row["prev_close"] <= 100 or len(g) < 60):
                continue
            g = g.reset_index(drop=True)
            if int(g["smin"].iloc[0]) != 555:
                continue
            o0 = float(g["open"].iloc[0])
            h0 = float(g["high"].iloc[0])
            l0 = float(g["low"].iloc[0])
            c0 = float(g["close"].iloc[0])
            v0 = float(g["volume"].iloc[0])
            if c0 == o0 or v0 <= 0 or h0 <= l0:
                continue
            rvol = v0 / float(row["avg_open5_vol"])
            if rvol < 1.0:
                continue
            arr = {k: g[k].to_numpy() for k in ("open", "high", "low", "close", "smin", "vwap")}
            days[(sym, dt)] = arr
            setups.append(dict(
                date=dt,
                sym=sym,
                bias="BULL" if c0 > o0 else "BEAR",
                rvol=rvol,
                o0=o0,
                h0=h0,
                l0=l0,
                c0=c0,
                atr14=float(row["atr14"]),
                nifty_open=nifty_map.get((dt, 555), 0.0),
            ))
    return setups, days


# ---------------------------------------------------------------- simulate
def bucket_of(smin):
    if smin < 585:
        return "09:20-09:45"
    if smin < 660:
        return "09:45-11:00"
    return "11:00-14:00"


def simulate(s, arr, p):
    side = 1 if s["bias"] == "BULL" else -1
    trigger = s["h0"] if side == 1 else s["l0"]
    n = len(arr["close"])

    entry_idx, entry_px = None, None
    for j in range(1, n):
        sm = int(arr["smin"][j])
        if sm > 840:  # 14:00 ke baad naya breakout entry nahi
            break
        o = float(arr["open"][j])
        hi = float(arr["high"][j])
        lo = float(arr["low"][j])
        if side == 1 and hi >= trigger:
            entry_idx = j
            entry_px = max(o, trigger)
            break
        if side == -1 and lo <= trigger:
            entry_idx = j
            entry_px = min(o, trigger)
            break

    if entry_idx is None:
        return None

    if p["sl_mode"] == "or":
        sl0 = s["l0"] if side == 1 else s["h0"]
        risk = max(abs(entry_px - sl0), entry_px * 0.0025, 0.5)
    else:
        risk = max(0.25 * s["atr14"], entry_px * 0.0025, 0.5)

    sl = entry_px - side * risk
    tgt = (entry_px + side * risk * p["tgt_r"]) if p["tgt_r"] else None

    exit_px, reason = None, "EOD"
    last_close = float(arr["close"][entry_idx])

    # Conservative same-bar check (SL pehle, phir TGT)
    e_hi = float(arr["high"][entry_idx])
    e_lo = float(arr["low"][entry_idx])
    if side == 1:
        if e_lo <= sl:
            exit_px, reason = sl, "SL"
        elif tgt is not None and e_hi >= tgt:
            exit_px, reason = tgt, "TGT"
    else:
        if e_hi >= sl:
            exit_px, reason = sl, "SL"
        elif tgt is not None and e_lo <= tgt:
            exit_px, reason = tgt, "TGT"

    if exit_px is None:
        for k in range(entry_idx + 1, n):
            sm = int(arr["smin"][k])
            if sm > 910:  # last candle start 15:10 (15:15 square-off)
                break
            hi = float(arr["high"][k])
            lo = float(arr["low"][k])
            cl = float(arr["close"][k])
            vw = float(arr["vwap"][k])
            if side == 1:
                if lo <= sl:
                    exit_px, reason = sl, "SL"
                    break
                if tgt is not None and hi >= tgt:
                    exit_px, reason = tgt, "TGT"
                    break
                if p["vwap_exit"] and sm >= 600 and not np.isnan(vw) and cl < vw:
                    exit_px, reason = cl, "VWAP"
                    break
            else:
                if hi >= sl:
                    exit_px, reason = sl, "SL"
                    break
                if tgt is not None and lo <= tgt:
                    exit_px, reason = tgt, "TGT"
                    break
                if p["vwap_exit"] and sm >= 600 and not np.isnan(vw) and cl > vw:
                    exit_px, reason = cl, "VWAP"
                    break
            last_close = cl

    if exit_px is None:
        exit_px = last_close

    r_gross = (exit_px - entry_px) * side / risk
    r_net = r_gross - (entry_px * COST_PCT / 100.0) / risk
    esm = int(arr["smin"][entry_idx])
    return dict(
        date=str(s["date"]),
        time=f"{esm // 60:02d}:{esm % 60:02d}",
        sym=s["sym"],
        side="LONG" if side == 1 else "SHORT",
        rvol=round(s["rvol"], 2),
        entry=round(entry_px, 2),
        sl=round(sl, 2),
        target=round(tgt, 2) if tgt is not None else "",
        exit=round(exit_px, 2),
        reason=reason,
        R=round(r_net, 3),
        bucket=bucket_of(esm),
    )


def run_variant(setups, days, p):
    by_day = defaultdict(list)
    for s in setups:
        by_day[s["date"]].append(s)
    trades = []
    for dt in sorted(by_day):
        cands = []
        for s in by_day[dt]:
            if s["rvol"] < p["rvol"]:
                continue
            if p["nifty"]:
                if s["bias"] == "BULL" and s["nifty_open"] <= -0.2:
                    continue
                if s["bias"] == "BEAR" and s["nifty_open"] >= 0.2:
                    continue
            cands.append(s)
        cands.sort(key=lambda x: -x["rvol"])
        for s in cands[:p["top_n"]]:
            t = simulate(s, days[(s["sym"], dt)], p)
            if t is not None:
                trades.append(t)
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


def build_report(results, period_txt, n_syms, split_date):
    lines = [f"5m ORB BACKTEST {period_txt} | {n_syms} stocks | cost {COST_PCT}%", ""]
    for key, name, _ in VARIANTS:
        lines.append(f"{key} {name}: {fmt(stats(results[key]))}")

    for key, name, _ in (VARIANTS[0], VARIANTS[3]):
        tr = results[key]
        lines += ["", f"{key} {name} - entry time & side:"]
        for b in ("09:20-09:45", "09:45-11:00", "11:00-14:00"):
            lines.append(f" {b}: {fmt(stats([t for t in tr if t['bucket'] == b]))}")
        for sd in ("LONG", "SHORT"):
            lines.append(f" {sd}: {fmt(stats([t for t in tr if t['side'] == sd]))}")

    sd = str(split_date)
    lines += ["", f"SPLIT: H1 = {sd} se pehle, H2 = {sd} ya uske baad"]
    for key, name, _ in VARIANTS:
        tr = results[key]
        h1 = [t for t in tr if t["date"] < sd]
        h2 = [t for t in tr if t["date"] >= sd]
        lines.append(f"{key} {name}")
        lines.append(f" H1: {fmt(stats(h1))}")
        lines.append(f" H2: {fmt(stats(h2))}")

    for label, key, side in (("A + LONG", "A", "LONG"), ("A + SHORT", "A", "SHORT"),
                             ("D + LONG", "D", "LONG"), ("D + SHORT", "D", "SHORT")):
        sel = [t for t in results[key] if t["side"] == side]
        lines.append(label)
        lines.append(f" H1: {fmt(stats([t for t in sel if t['date'] < sd]))}")
        lines.append(f" H2: {fmt(stats([t for t in sel if t['date'] >= sd]))}")

    best_key = max(VARIANTS, key=lambda v: stats(results[v[0]])["tot"])[0]
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

    setups, days = build_setups(data, nifty_map, first_date)
    print(f"Total daily ORB setups (RVOL>=1.0): {len(setups)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    results = {}
    for key, name, p in VARIANTS:
        tr = run_variant(setups, days, p)
        results[key] = tr
        pd.DataFrame(tr).to_csv(f"{OUT_DIR}/trades_{key}.csv", index=False)

    sd_env = os.getenv("SPLIT_DATE", "").strip()
    if sd_env:
        split_date = datetime.strptime(sd_env, "%Y-%m-%d").date()
    else:
        split_date = first_date + (now.date() - first_date) / 2
    report = build_report(results, f"{first_date} se {now.date()}", len(data), split_date)
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
    
