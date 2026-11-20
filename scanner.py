import os
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pyotp
import requests
from fyers_apiv3 import fyersModel

IST = timezone(timedelta(hours=5, minutes=30))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "/tmp/alerted_stocks.txt"
TOKEN_CACHE_FILE = "/tmp/fyers_access_token.txt"
TV_SCAN_URL = "https://scanner.tradingview.com/india/scan"


def get_ist_now():
    return datetime.now(IST)


def load_state(today_str):
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]
    if not lines or lines[0] != today_str:
        return set()
    return set(lines[1:])


def save_state(today_str, alerted_set):
    with open(STATE_FILE, "w") as f:
        f.write(today_str + "\n")
        for item in sorted(alerted_set):
            f.write(item + "\n")


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Telegram credentials missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        res = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        print(f"Telegram status: {res.status_code}")
    except requests.RequestException as e:
        print(f"Telegram send error: {e}")


def get_fyers_token(today_str):
    """Cached daily token uthata hai, warna TOTP se fresh login karta hai."""
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE, "r") as f:
            cached = f.read().strip().split("|")
            if len(cached) == 2 and cached[0] == today_str and cached[1]:
                return cached[1]

    fy_id = os.environ["FYERS_ID"].strip()
    totp_key = os.environ["FYERS_TOTP_KEY"].strip()
    pin = str(os.environ["FYERS_PIN"]).strip()
    app_id = os.environ["FYERS_APP_ID"].strip()
    secret_id = os.environ["FYERS_SECRET_ID"].strip()
    redirect_uri = "https://trade.fyers.in/api-login/redirect-uri/index.html"

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    s = requests.Session()

    r1 = s.post(
        "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
        json={"fy_id": fy_id, "app_id": "2"},
        headers=headers,
        timeout=15,
    ).json()
    if "request_key" not in r1:
        raise RuntimeError(f"Fyers Step 1 failed: {r1}")

    otp = pyotp.TOTP(totp_key).now()
    r2 = s.post(
        "https://api-t2.fyers.in/vagator/v2/verify_otp",
        json={"request_key": r1["request_key"], "otp": otp},
        headers=headers,
        timeout=15,
    ).json()
    if "request_key" not in r2:
        raise RuntimeError(f"Fyers Step 2 failed: {r2}")

    r3 = s.post(
        "https://api-t2.fyers.in/vagator/v2/verify_pin_v2",
        json={"request_key": r2["request_key"], "identity_type": "pin", "identifier": pin},
        headers=headers,
        timeout=15,
    ).json()
    if "data" not in r3 or "access_token" not in r3["data"]:
        raise RuntimeError(f"Fyers Step 3 failed: {r3}")

    auth_headers = {**headers, "authorization": f"Bearer {r3['data']['access_token']}"}
    client_prefix = app_id.split("-")[0]
    app_type = app_id.split("-")[1] if "-" in app_id else "100"

    r4 = s.post(
        "https://api-t1.fyers.in/api/v3/token",
        json={
            "fyers_id": fy_id,
            "app_id": client_prefix,
            "redirect_uri": redirect_uri,
            "appType": app_type,
            "code_challenge": "",
            "state": "sample_state",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True,
        },
        headers=auth_headers,
        timeout=15,
    ).json()
    if "Url" not in r4:
        raise RuntimeError(f"Fyers Step 4 failed: {r4}")

    auth_code = parse_qs(urlparse(r4["Url"]).query)["auth_code"][0]

    session_model = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    session_model.set_token(auth_code)
    token_resp = session_model.generate_token()

    if token_resp.get("s") != "ok":
        raise RuntimeError(f"Fyers Step 5 failed: {token_resp}")

    access_token = token_resp["access_token"]
    with open(TOKEN_CACHE_FILE, "w") as f:
        f.write(f"{today_str}|{access_token}")

    return access_token


def fetch_tv_universe(elapsed_fraction):
    """STAGE 0: Sirf Top 100 Liquid + High RVOL symbols nikaalta hai (0 Fyers calls)."""
    payload = {
        "filter": [
            {"left": "exchange", "operation": "equal", "right": "NSE"},
            {"left": "type", "operation": "equal", "right": "stock"},
            {"left": "market_cap_basic", "operation": "greater", "right": 50000000000},
            {"left": "close", "operation": "greater", "right": 100},
            {"left": "volume", "operation": "greater", "right": 50000},
        ],
        "options": {"lang": "en"},
        "markets": ["india"],
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "columns": ["name", "relative_volume_10d_calc"],
        "sort": {"sortBy": "relative_volume_10d_calc", "sortOrder": "desc"},
        "range": [0, 100],
    }
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    rvol_map = {}
    try:
        res = requests.post(TV_SCAN_URL, json=payload, headers=headers, timeout=15)
        if res.status_code == 200:
            for item in res.json().get("data", []):
                d = item.get("d", [])
                if len(d) >= 2 and d[0] and d[1] is not None:
                    proj_rvol = round(float(d[1]) / elapsed_fraction, 2)
                    if proj_rvol >= 1.5:
                        rvol_map[d[0]] = proj_rvol
    except requests.RequestException as e:
        print(f"TV Universe error: {e}")
    return rvol_map


def main():
    ist_now = get_ist_now()

    # 1. Weekend Guard
    if ist_now.weekday() >= 5:
        print("Weekend detected. Skipping scan.")
        return

    today_str = ist_now.strftime("%Y-%m-%d")
    time_str = ist_now.strftime("%H:%M")

    # 2. Intraday Time Window Guard (09:25 AM to 02:15 PM IST)
    if not ("09:25" <= time_str <= "14:15"):
        print(f"[{time_str} IST] Outside Intraday Window (09:25 - 14:15). Skipping.")
        return

    now_mins = ist_now.hour * 60 + ist_now.minute
    elapsed_fraction = max(min((now_mins - 555) / 375.0, 1.0), 0.04)

    # STAGE 0: High-RVOL Universe
    rvol_map = fetch_tv_universe(elapsed_fraction)
    if not rvol_map:
        print(f"[{time_str} IST] No high-RVOL stocks found.")
        return

    # Fyers Init
    try:
        app_id = os.environ["FYERS_APP_ID"].strip()
        access_token = get_fyers_token(today_str)
        fyers = fyersModel.FyersModel(client_id=app_id, token=access_token, log_path="")
    except Exception as e:
        err_msg = f"⚠️ <b>Fyers Login Error ({time_str} IST):</b>\n{e}"
        print(err_msg)
        send_telegram(err_msg)
        return

    alerted = load_state(today_str)
    fyers_syms = [f"NSE:{sym}-EQ" for sym in rvol_map.keys()]

    # STAGE 1: Real-Time Bulk Quotes (50 per call -> Max 2 calls)
    shortlisted = []
    for i in range(0, len(fyers_syms), 50):
        batch = ",".join(fyers_syms[i : i + 50])
        q_resp = fyers.quotes(data={"symbols": batch})
        time.sleep(0.25)

        if q_resp.get("s") != "ok":
            continue

        for item in q_resp.get("d", []):
            v = item.get("v", {})
            sym_full = item.get("n", "")
            clean_sym = sym_full.replace("NSE:", "").replace("-EQ", "")

            ltp = float(v.get("lp", 0))
            day_o = float(v.get("open_price", 0))
            day_h = float(v.get("high_price", 0))
            day_l = float(v.get("low_price", 0))
            chg = round(float(v.get("chp", 0)), 2)

            if ltp <= 0 or day_o <= 0:
                continue

            # Directional pre-filter before calling history()
            is_bull_cand = chg >= 1.0 and ltp > day_o and ltp < (day_h * 0.997)
            is_bear_cand = chg <= -1.0 and ltp < day_o and ltp > (day_l * 1.003)

            if is_bull_cand or is_bear_cand:
                shortlisted.append({
                    "fyers_sym": sym_full,
                    "sym": clean_sym,
                    "proj_rvol": rvol_map.get(clean_sym, 1.5),
                    "ltp": ltp,
                    "chg": chg,
                    "day_o": day_o,
                    "day_h": day_h,
                    "day_l": day_l,
                    "bias": "BULL" if is_bull_cand else "BEAR",
                })

    # Top 20 highest-RVOL movers hi Stage 2 mein jayenge (Strict Rate-Limit Guard)
    shortlisted.sort(key=lambda x: x["proj_rvol"], reverse=True)
    shortlisted = shortlisted[:20]

    # STAGE 2: Zero-Delay 5m Candles + Exact Local VWAP
    market_open = ist_now.replace(hour=9, minute=15, second=0, microsecond=0)
    last_closed = ist_now - timedelta(
        minutes=ist_now.minute % 5, seconds=ist_now.second, microseconds=ist_now.microsecond
    )

    candidates = []
    for st in shortlisted:
        hist_req = {
            "symbol": st["fyers_sym"],
            "resolution": "5",
            "date_format": "0",
            "range_from": str(int(market_open.timestamp())),
            "range_to": str(int(last_closed.timestamp())),
            "cont_flag": "1",
        }
        h_resp = fyers.history(data=hist_req)
        time.sleep(0.25)  # 4 req/sec max (Limit: 5/sec)

        if h_resp.get("s") != "ok" or not h_resp.get("candles"):
            continue

        df = pd.DataFrame(h_resp["candles"], columns=["ts", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")

        # Lag Kill-Switch: 10 min se purani candle reject karo
        latest_time = df["time"].iloc[-1].to_pydatetime()
        if (ist_now - latest_time).total_seconds() > 600:
            continue

        # Exact Intraday VWAP
        cum_vol = df["volume"].cumsum()
        if cum_vol.iloc[-1] <= 0:
            continue
        tp = (df["high"] + df["low"] + df["close"]) / 3
        df["vwap"] = (tp * df["volume"]).cumsum() / cum_vol

        last = df.iloc[-1]
        sym = st["sym"]
        proj_rvol = st["proj_rvol"]
        chg = st["chg"]

        cmp_p = round(float(last["close"]), 2)
        open_5m = float(last["open"])
        high_5m = float(last["high"])
        low_5m = float(last["low"])
        vwap_5m = round(float(last["vwap"]), 2)
        candle_str = latest_time.strftime("%H:%M")

        if vwap_5m <= 0:
            continue

        vwap_dist_pct = ((cmp_p - vwap_5m) / vwap_5m) * 100

        # BULLISH 5m VWAP PULLBACK + GREEN BOUNCE
        if (
            st["bias"] == "BULL"
            and (0.02 <= vwap_dist_pct <= 0.45)
            and low_5m <= (vwap_5m * 1.003)
            and cmp_p > open_5m
        ):
            key = f"BULL_{sym}"
            if key not in alerted:
                sl = round(min(low_5m, vwap_5m) * 0.997, 2)
                risk = max(round(cmp_p - sl, 2), 0.5)
                tgt = round(cmp_p + (risk * 2.5), 2)
                msg = (
                    f"🟢 <b>LONG (5m VWAP Bounce): {sym}</b>\n"
                    f"   CMP: ₹{cmp_p} (+{chg}%) | Proj RVOL: <b>{proj_rvol}x</b>\n"
                    f"   5m VWAP: ₹{vwap_5m} | Candle: {candle_str} (Green)\n"
                    f"   SL: ₹{sl} | Target (2.5R): ₹{tgt}"
                )
                candidates.append((proj_rvol, key, msg))

        # BEARISH 5m VWAP REJECTION + RED CANDLE
        elif (
            st["bias"] == "BEAR"
            and (-0.45 <= vwap_dist_pct <= -0.02)
            and high_5m >= (vwap_5m * 0.997)
            and cmp_p < open_5m
        ):
            key = f"BEAR_{sym}"
            if key not in alerted:
                sl = round(max(high_5m, vwap_5m) * 1.003, 2)
                risk = max(round(sl - cmp_p, 2), 0.5)
                tgt = round(cmp_p - (risk * 2.5), 2)
                msg = (
                    f"🔴 <b>SHORT (5m VWAP Rejection): {sym}</b>\n"
                    f"   CMP: ₹{cmp_p} ({chg}%) | Proj RVOL: <b>{proj_rvol}x</b>\n"
                    f"   5m VWAP: ₹{vwap_5m} | Candle: {candle_str} (Red)\n"
                    f"   SL: ₹{sl} | Target (2.5R): ₹{tgt}"
                )
                candidates.append((proj_rvol, key, msg))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        top_alerts = candidates[:5]
        alert_texts = []
        for _, key, msg in top_alerts:
            alerted.add(key)
            alert_texts.append(msg)

        header = f"⚡ <b>LIVE 5m VWAP BOUNCE ({time_str} IST)</b> ⚡\n\n"
        send_telegram(header + "\n\n".join(alert_texts))
        print(f"[{time_str} IST] Dispatched {len(top_alerts)} live alerts.")
    else:
        print(f"[{time_str} IST] Scanned {len(shortlisted)} live movers. No confirmed setup.")

    save_state(today_str, alerted)


if __name__ == "__main__":
    main()
                
