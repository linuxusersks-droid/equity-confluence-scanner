import hashlib
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pyotp
import requests

IST = timezone(timedelta(hours=5, minutes=30))
TEST_SYMBOLS = [
    "EXIDEIND", "ETERNAL", "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "SBIN", "AXISBANK", "LT", "ITC", "SUNPHARMA", "M&M", "BHARTIARTL", "KOTAKBANK"
]


def get_fyers_token() -> str:
    fy_id = os.environ["FYERS_ID"].strip()
    totp_key = os.environ["FYERS_TOTP_KEY"].strip()
    pin = str(os.environ["FYERS_PIN"]).strip()
    app_id = os.environ["FYERS_APP_ID"].strip()
    secret_id = os.environ["FYERS_SECRET_ID"].strip()
    redirect_uri = "https://trade.fyers.in/api-login/redirect-uri/index.html"

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    s = requests.Session()

    r1 = s.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2", json={"fy_id": fy_id, "app_id": "2"}, headers=headers, timeout=15).json()
    otp = pyotp.TOTP(totp_key).now()
    r2 = s.post("https://api-t2.fyers.in/vagator/v2/verify_otp", json={"request_key": r1["request_key"], "otp": otp}, headers=headers, timeout=15).json()
    r3 = s.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2", json={"request_key": r2["request_key"], "identity_type": "pin", "identifier": pin}, headers=headers, timeout=15).json()

    auth_headers = {**headers, "authorization": f"Bearer {r3['data']['access_token']}"}
    client_prefix = app_id.split("-")[0]
    app_type = app_id.split("-")[1] if "-" in app_id else "100"

    r4 = s.post(
        "https://api-t1.fyers.in/api/v3/token",
        json={
            "fyers_id": fy_id, "app_id": client_prefix, "redirect_uri": redirect_uri,
            "appType": app_type, "code_challenge": "", "state": "sample_state",
            "scope": "", "nonce": "", "response_type": "code", "create_cookie": True,
        },
        headers=auth_headers, timeout=15,
    ).json()
    auth_code = parse_qs(urlparse(r4["Url"]).query)["auth_code"][0]

    app_id_hash = hashlib.sha256(f"{app_id}:{secret_id}".encode()).hexdigest()
    r5 = s.post(
        "https://api-t1.fyers.in/api/v3/validate-authcode",
        json={"grant_type": "authorization_code", "appIdHash": app_id_hash, "code": auth_code},
        headers=headers, timeout=15,
    ).json()

    if r5.get("s") != "ok":
        raise RuntimeError(f"Token exchange failed: {r5}")
    return r5["access_token"]


def fetch_6m_5m_df(app_id: str, token: str, fyers_sym: str, end_dt: datetime) -> pd.DataFrame:
    start_dt = end_dt - timedelta(days=183)
    chunks = []
    cur_start = start_dt
    headers = {"Authorization": f"{app_id}:{token}"}

    while cur_start < end_dt:
        cur_end = min(cur_start + timedelta(days=90), end_dt)
        params = {
            "symbol": fyers_sym, "resolution": "5", "date_format": "0",
            "range_from": str(int(cur_start.timestamp())),
            "range_to": str(int(cur_end.timestamp())), "cont_flag": "1",
        }
        resp = requests.get("https://api-t1.fyers.in/data/history", headers=headers, params=params, timeout=15).json()
        time.sleep(0.25)
        if resp.get("s") == "ok" and resp.get("candles"):
            chunks.append(pd.DataFrame(resp["candles"], columns=["ts", "open", "high", "low", "close", "volume"]))
        cur_start = cur_end + timedelta(seconds=1)

    if not chunks:
        return pd.DataFrame()

    df = pd.concat(chunks, ignore_index=True).drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(IST)
    df["date"] = df["dt"].dt.date
    df["hm"] = df["dt"].dt.strftime("%H:%M")
    df = df[(df["hm"] >= "09:15") & (df["hm"] <= "15:25") & (df["volume"] > 0)].copy()

    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["tpv"] = tp * df["volume"]
    df["vwap"] = df.groupby("date")["tpv"].cumsum() / df.groupby("date")["volume"].cumsum()
    df["day_o"] = df.groupby("date")["open"].transform("first")
    df["day_h"] = df.groupby("date")["high"].cummax()
    df["day_l"] = df.groupby("date")["low"].cummin()
    df["chg_from_open"] = ((df["close"] - df["day_o"]) / df["day_o"]) * 100
    return df


def simulate_symbol(sym: str, df: pd.DataFrame) -> list[dict]:
    trades = []
    for trade_date, g in df.groupby("date"):
        g = g.reset_index(drop=True)
        taken_bull = taken_bear = False

        for i in range(1, len(g)):
            row = g.iloc[i]
            if not ("09:25" <= row["hm"] <= "14:15"):
                continue

            cmp_p, open_5m, high_5m, low_5m = float(row["close"]), float(row["open"]), float(row["high"]), float(row["low"])
            vwap_5m, day_o, day_h, day_l, chg = float(row["vwap"]), float(row["day_o"]), float(row["day_h"]), float(row["day_l"]), float(row["chg_from_open"])
            if vwap_5m <= 0:
                continue

            vwap_dist_pct = ((cmp_p - vwap_5m) / vwap_5m) * 100
            side = sl = tgt = None

            if not taken_bull and chg >= 1.0 and cmp_p > day_o and (0.02 <= vwap_dist_pct <= 0.45) and low_5m <= (vwap_5m * 1.003) and cmp_p > open_5m and cmp_p < (day_h * 0.997):
                side = "BULL"
                sl = round(min(low_5m, vwap_5m) * 0.997, 2)
                tgt = round(cmp_p + (max(round(cmp_p - sl, 2), 0.5) * 2.5), 2)
                taken_bull = True
            elif not taken_bear and chg <= -1.0 and cmp_p < day_o and (-0.45 <= vwap_dist_pct <= -0.02) and high_5m >= (vwap_5m * 0.997) and cmp_p < open_5m and cmp_p > (day_l * 1.003):
                side = "BEAR"
                sl = round(max(high_5m, vwap_5m) * 1.003, 2)
                tgt = round(cmp_p - (max(round(sl - cmp_p, 2), 0.5) * 2.5), 2)
                taken_bear = True

            if side is None:
                continue

            outcome, pnl_pts = "EOD", 0.0
            for j in range(i + 1, len(g)):
                nh, nl = float(g.iloc[j]["high"]), float(g.iloc[j]["low"])
                if side == "BULL":
                    if nl <= sl:
                        outcome, pnl_pts = "LOSS", round(sl - cmp_p, 2); break
                    if nh >= tgt:
                        outcome, pnl_pts = "WIN", round(tgt - cmp_p, 2); break
                else:
                    if nh >= sl:
                        outcome, pnl_pts = "LOSS", round(cmp_p - sl, 2); break
                    if nl <= tgt:
                        outcome, pnl_pts = "WIN", round(cmp_p - tgt, 2); break

            if outcome == "EOD":
                eod_c = float(g.iloc[-1]["close"])
                pnl_pts = round((eod_c - cmp_p) if side == "BULL" else (cmp_p - eod_c), 2)

            r_mult = round(pnl_pts / max(abs(cmp_p - sl), 0.01), 2)
            trades.append({"date": str(trade_date), "time": row["hm"], "sym": sym, "side": side, "outcome": outcome, "r_mult": r_mult})
    return trades


def main():
    now_ist = datetime.now(IST)
    app_id = os.environ["FYERS_APP_ID"].strip()
    token = get_fyers_token()

    all_trades = []
    for sym in TEST_SYMBOLS:
        df = fetch_6m_5m_df(app_id, token, f"NSE:{sym}-EQ", now_ist)
        if not df.empty:
            t = simulate_symbol(sym, df)
            all_trades.extend(t)
            print(f"{sym}: {len(t)} trades")

    if not all_trades:
        print("No trades triggered.")
        return

    res = pd.DataFrame(all_trades)
    wins = (res["outcome"] == "WIN").sum()
    losses = (res["outcome"] == "LOSS").sum()
    eods = (res["outcome"] == "EOD").sum()
    win_rate = round((wins / max(wins + losses, 1)) * 100, 2)

    print("\n=== 6-MONTH 5m VWAP BACKTEST SUMMARY ===")
    print(f"Total Setups : {len(res)} | Wins: {wins} | Losses: {losses} | EOD: {eods}")
    print(f"Win Rate (W/L): {win_rate}% | Net R: {round(res['r_mult'].sum(), 2)}R | Avg R/Trade: {round(res['r_mult'].mean(), 2)}R")
    print("\nBy Side:")
    print(res.groupby("side")["r_mult"].agg(["count", "sum", "mean"]))


if __name__ == "__main__":
    main()
