"""
5m VWAP scanner backtest (scanner.py ki logic ka replica).
- Fyers se 5m candles leta hai, scanner ke 3 stage simulate karta hai
- Alag-alag filter variants compare karta hai
- Summary Telegram par bhejta hai, trades CSV backtest_out/ me save karta hai
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = "backtest_out"
COLS = ["ts", "open", "high", "low", "close", "volume"]

BACKTEST_DAYS = int(os.getenv("BACKTEST_DAYS", "180") or "180")
WARMUP_DAYS = 25
COST_PCT = float(os.getenv("COST_PCT", "0.05") or "0.05")  # round-trip cost % (assumption)

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
    ("A", "Scanner (abhi wala)", dict(nifty=True, trend=False, sl_mode="scanner", tgt_r=2.5)),
    ("B", "Bina Nifty filter", dict(nifty=False, trend=False, sl_mode="scanner", tgt_r=2.5)),
    ("C", "Trend filter", dict(nifty=True, trend=True, sl_mode="scanner", tgt_r=2.5)),
    ("D", "Wide SL + 2R", dict(nifty=True, trend=False, sl_mode="wide", tgt_r=2.0)),
    ("E", "Trend + Wide SL + 2R", dict(nifty=True, trend=True, sl_mode="wide", tgt_r=2.0)),
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
            except Exception as e:  # noqa
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


def daily_stats(df):
    d = df.groupby("date").agg(
        close=("close", "last"), vol=("volume", "sum"), n=("ts", "count")
    ).sort_index()
    d["prev_close"] = d["close"].shift(1)
    d["avg10"] = d["vol"].shift(1).rolling(10, min_periods=5).mean()
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


# ---------------------------------------------------------------- records
def build_records(data, nifty_map, first_date):
    """Har symbol/din/candle ke liye stage 0-1 pass hone par record banata hai."""
    records, days = [], {}
    for sym, df in data.items():
        ds = daily_stats(df)
        for dt, g in df.groupby("date"):
            if dt < first_date or dt not in ds.index:
                continue
            row = ds.loc[dt]
            if (pd.isna(row["prev_close"]) or pd.isna(row["avg10"])
                    or row["avg10"] <= 0 or row["prev_close"] <= 100 or len(g) < 60):
                continue
            g = g.reset_index(drop=True)
            arr = {k: g[k].to_numpy() for k in ("open", "high", "low", "close", "smin", "vwap")}
            days[(sym, dt)] = arr
            cum_h = np.maximum.accumulate(arr["high"])
            cum_l = np.minimum.accumulate(arr["low"])
            cumvol = g["cumvol"].to_numpy()
            day_o = arr["open"][0]
            prev = row["prev_close"]
            for i in range(len(g)):
                scan_min = int(arr["smin"][i]) + 5
                if scan_min < 565 or scan_min > 855:  # 09:25 - 14:15
                    continue
                elapsed = max(min((scan_min - 555) / 375.0, 1.0), 0.04)
                proj = cumvol[i] / row["avg10"] / elapsed
                if proj < 1.5:
                    continue
                ltp = arr["close"][i]
                chg = (ltp - prev) / prev * 100
                bull = chg >= 1.0 and ltp > day_o and ltp < cum_h[i] * 0.997
                bear = chg <= -1.0 and ltp < day_o and ltp > cum_l[i] * 1.003
                if not (bull or bear):
                    continue
                vw = arr["vwap"][i]
                if np.isnan(vw) or vw <= 0:
                    continue
                o5, h5, l5 = arr["open"][i], arr["high"][i], arr["low"][i]
                dist = (ltp - vw) / vw * 100
                if bull:
                    bias = "BULL"
                    sig = 0.02 <= dist <= 0.45 and l5 <= vw * 1.003 and ltp > o5
                else:
                    bias = "BEAR"
                    sig = -0.45 <= dist <= -0.02 and h5 >= vw * 0.997 and ltp < o5
                trend_ok = False
                if i >= 6:
                    cl = arr["close"][i - 3:i + 1]
                    vl = arr["vwap"][i - 3:i + 1]
                    if bull:
                        trend_ok = (cl > vl).sum() >= 3 and l5 > arr["low"][i - 6:i].min()
                    else:
                        trend_ok = (cl < vl).sum() >= 3 and h5 < arr["high"][i - 6:i].max()
                lo3 = arr["low"][max(0, i - 2):i + 1].min()
                hi3 = arr["high"][max(0, i - 2):i + 1].max()
                records.append(dict(
                    date=dt, scan_min=scan_min, sym=sym, bias=bias, proj=proj,
                    sig=bool(sig), trend_ok=bool(trend_ok), i=i, entry=ltp,
                    l5=l5, h5=h5, vwap=vw, sw_low=lo3, sw_high=hi3,
                    nifty=nifty_map.get((dt, int(arr["smin"][i])), 0.0),
                ))
    return records, days


# ---------------------------------------------------------------- simulate
def bucket_of(scan_min):
    if scan_min < 630:
        return "09:25-10:30"
    if scan_min < 720:
        return "10:30-12:00"
    return "12:00-14:15"


def simulate(r, arr, p):
    entry, vw = r["entry"], r["vwap"]
    side = 1 if r["bias"] == "BULL" else -1
    if p["sl_mode"] == "scanner":
        if side == 1:
            sl0 = round(min(r["l5"], vw) * 0.997, 2)
            risk = max(round(entry - sl0, 2), 0.5)
        else:
            sl0 = round(max(r["h5"], vw) * 1.003, 2)
            risk = max(round(sl0 - entry, 2), 0.5)
    else:
        if side == 1:
            base = min(r["sw_low"], vw) * 0.997
            risk = max(entry - base, entry * 0.005)
        else:
            base = max(r["sw_high"], vw) * 1.003
            risk = max(base - entry, entry * 0.005)
    sl = entry - side * risk
    tgt = entry + side * risk * p["tgt_r"]

    exit_px, reason, last_close = None, "TIME", entry
    n = len(arr["close"])
    for j in range(r["i"] + 1, n):
        if arr["smin"][j] > 910:  # last candle start 15:10
            break
        lo, hi = arr["low"][j], arr["high"][j]
        if side == 1:
            if lo <= sl:
                exit_px, reason = sl, "SL"
                break
            if hi >= tgt:
                exit_px, reason = tgt, "TGT"
                break
        else:
            if hi >= sl:
                exit_px, reason = sl, "SL"
                break
            if lo <= tgt:
                exit_px, reason = tgt, "TGT"
                break
        last_close = arr["close"][j]
    if exit_px is None:
        exit_px = last_close
    r_gross = (exit_px - entry) * side / risk
    r_net = r_gross - (entry * COST_PCT / 100.0) / risk
    sm = r["scan_min"]
    return dict(
        date=str(r["date"]), time=f"{sm // 60:02d}:{sm % 60:02d}", sym=r["sym"],
        side="LONG" if side == 1 else "SHORT", entry=round(entry, 2),
        sl=round(sl, 2), target=round(tgt, 2), exit=round(exit_px, 2),
        reason=reason, R=round(r_net, 3), bucket=bucket_of(sm),
    )


def run_variant(records, days, p):
    by_slot = defaultdict(list)
    for r in records:
        by_slot[(r["date"], r["scan_min"])].append(r)
    trades, alerted = [], defaultdict(set)
    for (dt, sm) in sorted(by_slot):
        cands = []
        for r in by_slot[(dt, sm)]:
            if p["nifty"]:
                if r["bias"] == "BULL" and r["nifty"] <= -0.2:
                    continue
                if r["bias"] == "BEAR" and r["nifty"] >= 0.2:
                    continue
            cands.append(r)
        cands.sort(key=lambda x: -x["proj"])
        cands = cands[:20]
        sig = [r for r in cands
               if r["sig"] and (not p["trend"] or r["trend_ok"])
               and (r["bias"], r["sym"]) not in alerted[dt]]
        sig.sort(key=lambda x: -x["proj"])
        for r in sig[:5]:
            alerted[dt].add((r["bias"], r["sym"]))
            trades.append(simulate(r, days[(r["sym"], dt)], p))
    return trades


# ---------------------------------------------------------------- report
def stats(trades):
    n = len(trades)
    if n == 0:
        return dict(n=0, win=0.0, avg=0.0, tot=0.0, pf=0.0, dd=0.0)
    R = np.array([t["R"] for t in trades])
    wins, losses = R[R > 0], R[R <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() < 0 else float("inf")
    cum = np.cumsum(R)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    return dict(n=n, win=len(wins) / n * 100, avg=float(R.mean()), tot=float(R.sum()), pf=float(pf), dd=dd)


def fmt(s):
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return f"n={s['n']} win={s['win']:.0f}% avgR={s['avg']:+.2f} totR={s['tot']:+.1f} PF={pf} DD={s['dd']:.1f}R"


def build_report(results, period_txt, n_syms, split_date):
    lines = [f"BACKTEST {period_txt} | {n_syms} stocks | cost {COST_PCT}%", ""]
    for key, name, _ in VARIANTS:
        lines.append(f"{key} {name}: {fmt(stats(results[key]))}")
    for key, name, _ in VARIANTS[:1] + VARIANTS[2:3]:
        tr = results[key]
        lines += ["", f"{key} {name} - time bucket:"]
        for b in ("09:25-10:30", "10:30-12:00", "12:00-14:15"):
            lines.append(f" {b}: {fmt(stats([t for t in tr if t['bucket'] == b]))}")
        for sd in ("LONG", "SHORT"):
            lines.append(f" {sd}: {fmt(stats([t for t in tr if t['side'] == sd]))}")
    # ---- Out-of-sample split: pehla hissa vs baad ka hissa
    sd = str(split_date)
    lines += ["", f"SPLIT: pehla hissa (< {sd}) vs baad ka hissa (>= {sd})"]
    for key, name, _ in VARIANTS:
        tr = results[key]
        h1 = [t for t in tr if t["date"] < sd]
        h2 = [t for t in tr if t["date"] >= sd]
        lines.append(f"{key} {name}")
        lines.append(f" H1: {fmt(stats(h1))}")
        lines.append(f" H2: {fmt(stats(h2))}")
    tr = results["C"]
    combos = [
        ("C + 10:30-12:00", lambda t: t["bucket"] == "10:30-12:00"),
        ("C + SHORT", lambda t: t["side"] == "SHORT"),
        ("C + SHORT + 10:30-12:00", lambda t: t["side"] == "SHORT" and t["bucket"] == "10:30-12:00"),
    ]
    for label, f in combos:
        sel = [t for t in tr if f(t)]
        lines.append(label)
        lines.append(f" H1: {fmt(stats([t for t in sel if t['date'] < sd]))}")
        lines.append(f" H2: {fmt(stats([t for t in sel if t['date'] >= sd]))}")
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

    records, days = build_records(data, nifty_map, first_date)
    print(f"Stage-1 records: {len(records)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    results = {}
    for key, name, p in VARIANTS:
        tr = run_variant(records, days, p)
        results[key] = tr
        pd.DataFrame(tr).to_csv(f"{OUT_DIR}/trades_{key}.csv", index=False)

    split_date = first_date + (now.date() - first_date) / 2
    report = build_report(results, f"{first_date} se {now.date()}", len(data), split_date)
    print(report)
    with open(f"{OUT_DIR}/summary.txt", "w") as f:
        f.write(report)
    chunk = ""
    for ln in report.split("\n"):
        if len(chunk) + len(ln) + 1 > 3800:
            send_telegram(chunk)
            chunk = ""
        chunk += ln + "\n"
    if chunk.strip():
        send_telegram(chunk)


if __name__ == "__main__":
    main()
                           
