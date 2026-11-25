"""
Standalone Live Scanner: VWAP C + SHORT (09:25 - 12:00 IST)
- 100% self-contained: built-in Fyers v3 TOTP login + Telegram (no scanner.py needed)
- Sirf SHORT signals (09:25 se 12:00 IST)
- Rules: Nifty < +0.2%, Stock <= -1.0% & < Day Open, RVOL >= 1.5x,
  3/4 prior candles < VWAP + lower high, red VWAP rejection candle
- SL: max(candle high, VWAP) + 0.3% | Target: 2.5R | EOD exit: 3:15 PM
"""
import base64
import hashlib
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import pyotp
import requests

IST = timezone(timedelta(hours=5, minutes=30))
COLS = ["ts", "open", "high", "low", "close", "volume"]

MAX_SIGNALS = int(os.getenv("MAX_SIGNALS", "5") or "5")
TARGET_R = float(os.getenv("TARGET_R", "2.5") or "2.5")
RUN_MODE = os.getenv("RUN_MODE", "once").strip().lower()  # "once" ya "loop"
STATE_FILE = os.getenv("STATE_FILE", "vwap_short_state.json") or "vwap_short_state.json"

START_MIN = 565  # 09:25 IST
END_MIN = 720    # 12:00 IST

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


# ---------------- Standalone Auth & Telegram ----------------
def send_telegram(msg):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secrets missing, console only.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"Telegram HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print(f"Telegram error: {e}")


def get_fyers_token(today_str=""):
    cache_file = ".fyers_token_cache.json"
    if today_str and os.path.exists(cache_file):
        try:
            c = json.load(open(cache_file, "r", encoding="utf-8"))
            if c.get("date") == today_str and c.get("token"):
                return c["token"]
        except Exception:
            pass

    app_id = os.environ["FYERS_APP_ID"].strip()
    secret_key = os.environ["FYERS_SECRET_KEY"].strip()
    fy_id = os.environ["FYERS_ID"].strip()
    pin = os.environ["FYERS_PIN"].strip()
    totp_key = os.environ["FYERS_TOTP_KEY"].strip()

    s = requests.Session()
    b64 = lambda x: base64.b64encode(x.encode()).decode()

    r1 = s.post(
        "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
        json={"fy_id": b64(fy_id), "app_id": "2"},
        timeout=20,
    ).json()
    req_key = r1.get("request_key")
    if not req_key:
        raise RuntimeError(f"Fyers OTP init failed: {r1}")

    r2 = s.post(
        "https://api-t2.fyers.in/vagator/v2/verify_otp",
        json={"request_key": req_key, "otp": pyotp.TOTP(totp_key).now()},
        timeout=20,
    ).json()
    req_key = r2.get("request_key")
    if not req_key:
        raise RuntimeError(f"Fyers TOTP verify failed: {r2}")

    r3 = s.post(
        "https://api-t2.fyers.in/vagator/v2/verify_pin_v2",
        json={"request_key": req_key, "identity_type": "pin", "identifier": b64(pin)},
        timeout=20,
    ).json()
    bearer = (r3.get("data") or {}).get("access_token")
    if not bearer:
        raise RuntimeError(f"Fyers PIN verify failed: {r3}")

    app_prefix = app_id.split("-")[0]
    app_type = app_id.split("-")[1] if "-" in app_id else "100"
    redirect_candidates = []
    if os.getenv("FYERS_REDIRECT_URI"):
        redirect_candidates.append(os.getenv("FYERS_REDIRECT_URI").strip())
    redirect_candidates += [
        "https://trade.fyers.in/api-login/redirect-uri/index.html",
        "https://www.google.com",
        "https://www.google.com/",
        "https://127.0.0.1:5000",
        "https://127.0.0.1",
        "https://fyers.in",
    ]

    auth_code = None
    last_err = None
    for redir in redirect_candidates:
        r4 = s.post(
            "https://api-t1.fyers.in/api/v3/token",
            headers={"Authorization": f"Bearer {bearer}"},
            json={
                "fyers_id": fy_id,
                "app_id": app_prefix,
                "redirect_uri": redir,
                "appType": app_type,
                "code_challenge": "",
                "state": "None",
                "scope": "",
                "nonce": "",
                "response_type": "code",
                "create_cookie": True,
            },
            allow_redirects=False,
            timeout=20,
        ).json()
        url = r4.get("Url") or ""
        qs = parse_qs(urlparse(url).query)
        if "auth_code" in qs:
            auth_code = qs["auth_code"][0]
            break
        last_err = r4

    if not auth_code:
        raise RuntimeError(f"Fyers auth_code failed (set FYERS_REDIRECT_URI): {last_err}")

    app_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()
    r5 = s.post(
        "https://api-t1.fyers.in/api/v3/validate-authcode",
        json={"grant_type": "authorization_code", "appIdHash": app_hash, "code": auth_code},
        timeout=20,
    ).json()
    token = r5.get("access_token")
    if not token:
        raise RuntimeError(f"Fyers token exchange failed: {r5}")

    if today_str:
        try:
            json.dump({"date": today_str, "token": token}, open(cache_file, "w", encoding="utf-8"))
        except Exception:
            pass
    return token


# ---------------- Data & Strategy ----------------
def prepare_df(candles):
    if not candles:
        return None
    df = pd.DataFrame(candles, columns=COLS).drop_duplicates("ts").sort_values("ts").copy()
    t = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df["date"] = t.dt.date
    df["smin"] = t.dt.hour * 60 + t.dt.minute
    df = df[(df["smin"] >= 555) & (df["smin"] <= 925)].reset_index(drop=True)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    df["cumvol"] = df.groupby("date")["volume"].cumsum()
    df["vwap"] = (tp * df["volume"]).groupby(df["date"]).cumsum() / df["cumvol"].replace(0, np.nan)
    return df


def fetch_5m(fyers, fy_sym, now_dt, days=22):
    req = {
        "symbol": fy_sym,
        "resolution": "5",
        "date_format": "0",
        "range_from": str(int((now_dt - timedelta(days=days)).timestamp())),
        "range_to": str(int(now_dt.timestamp())),
        "cont_flag": "1",
    }
    for attempt in range(3):
        try:
            resp = fyers.history(data=req)
            if resp.get("s") == "ok" and resp.get("candles"):
                return prepare_df(resp["candles"])
        except Exception as e:
            if attempt == 2:
                print(f"  {fy_sym} fetch error: {e}")
        time.sleep(0.8)
    return None


def get_nifty_map(ndf, today, latest_smin):
    if ndf is None or ndf.empty:
        return {}, None
    hist = ndf[ndf["date"] < today]
    tod = ndf[(ndf["date"] == today) & (ndf["smin"] <= latest_smin)]
    if hist.empty or tod.empty:
        return {}, None
    prev_close = float(hist.groupby("date")["close"].last().iloc[-1])
    if prev_close <= 0:
        return {}, None
    chg_map = {
        int(sm): (float(cl) - prev_close) / prev_close * 100.0
        for sm, cl in zip(tod["smin"], tod["close"])
    }
    return chg_map, chg_map.get(int(tod["smin"].iloc[-1]))


def find_signal(sym, df, today, latest_smin, nifty_map):
    hist = df[df["date"] < today]
    tod = df[(df["date"] == today) & (df["smin"] <= latest_smin)].reset_index(drop=True)
    if hist.empty or len(tod) < 2:
        return None

    daily = hist.groupby("date").agg(close=("close", "last"), vol=("volume", "sum")).sort_index()
    if len(daily) < 5:
        return None
    prev_close = float(daily["close"].iloc[-1])
    avg10_vol = float(daily["vol"].tail(10).mean())
    if prev_close <= 100.0 or avg10_vol <= 0:
        return None

    o = tod["open"].to_numpy(dtype=float)
    h = tod["high"].to_numpy(dtype=float)
    c = tod["close"].to_numpy(dtype=float)
    vw = tod["vwap"].to_numpy(dtype=float)
    cv = tod["cumvol"].to_numpy(dtype=float)
    smin = tod["smin"].to_numpy(dtype=int)
    day_open = float(o[0])

    for i in range(1, len(tod)):
        sm = int(smin[i])
        scan_min = sm + 5
        if scan_min < START_MIN or scan_min > END_MIN:
            continue
        if np.isnan(vw[i]) or vw[i] <= 0:
            continue

        n_chg = nifty_map.get(sm, 0.0)
        if n_chg >= 0.2:
            continue

        chg_pct = (c[i] - prev_close) / prev_close * 100.0
        if chg_pct > -1.0 or c[i] >= day_open:
            continue

        elapsed = max(min((scan_min - 555) / 375.0, 1.0), 0.04)
        rvol = cv[i] / avg10_vol / elapsed
        if rvol < 1.5:
            continue

        p0 = max(0, i - 4)
        if int(np.sum(c[p0:i] < vw[p0:i])) < min(3, i - p0):
            continue
        if h[i] > float(np.max(h[p0:i])) * 1.0005:
            continue

        if c[i] >= o[i]:
            continue
        dist_pct = (vw[i] - c[i]) / vw[i] * 100.0
        if not (0.02 <= dist_pct <= 0.45) or h[i] < vw[i] * 0.9985:
            continue

        entry = float(c[i])
        sl = max(float(h[i]), float(vw[i])) * 1.003
        risk = sl - entry
        sl_pct = (risk / entry) * 100.0
        if risk <= 0 or sl_pct > 2.0:
            continue

        return dict(
            sym=sym,
            smin=sm,
            scan_min=scan_min,
            time_str=f"{scan_min // 60:02d}:{scan_min % 60:02d}",
            entry=round(entry, 2),
            sl=round(sl, 2),
            target=round(entry - TARGET_R * risk, 2),
            vwap=round(float(vw[i]), 2),
            sl_pct=round(sl_pct, 2),
            chg_pct=round(chg_pct, 2),
            rvol=round(rvol, 2),
            nifty_chg=round(n_chg, 2),
        )
    return None


def load_state(today_str):
    if os.path.exists(STATE_FILE):
        try:
            st = json.load(open(STATE_FILE, "r", encoding="utf-8"))
            if st.get("date") == today_str:
                return st
        except Exception:
            pass
    return {"date": today_str, "sent": []}


def save_state(state):
    try:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"))
    except Exception:
        pass


def format_alert(sig, idx):
    return "\n".join([
        f"🔴 <b>VWAP C SHORT [{idx}/{MAX_SIGNALS}] — {html.escape(sig['sym'])}</b>",
        f"⏰ <b>{sig['time_str']} IST</b> | Day: <b>{sig['chg_pct']}%</b> | RVOL: <b>{sig['rvol']}x</b>",
        f"📉 Nifty: <b>{sig['nifty_chg']:+.2f}%</b> | VWAP: <b>₹{sig['vwap']}</b>",
        "",
        f"🎯 Entry: <b>₹{sig['entry']}</b>",
        f"🛑 SL: <b>₹{sig['sl']}</b> ({sig['sl_pct']}%)",
        f"✅ Target ({TARGET_R}R): <b>₹{sig['target']}</b>",
        "⏳ <i>EOD exit: 3:15 PM</i>",
    ])


def get_symbols():
    if os.getenv("SYMBOLS"):
        return [s.strip().upper() for s in os.getenv("SYMBOLS").split(",") if s.strip()]
    if os.path.exists("symbols.txt"):
        return [s.strip().upper() for s in open("symbols.txt", encoding="utf-8") if s.strip()]
    return DEFAULT_SYMBOLS


def run_once(fyers, now_dt=None):
    now_dt = now_dt or datetime.now(IST)
    today = now_dt.date()
    cur_min = now_dt.hour * 60 + now_dt.minute
    close_min = (cur_min // 5) * 5
    latest_smin = close_min - 5

    if close_min < START_MIN or close_min > END_MIN + 5:
        print(f"[{now_dt.strftime('%H:%M:%S')}] Outside 09:25-12:00 IST window. Skip.")
        return

    print(f"[{now_dt.strftime('%H:%M:%S')}] Scanning {close_min // 60:02d}:{close_min % 60:02d} IST bar...")
    ndf = fetch_5m(fyers, "NSE:NIFTY50-INDEX", now_dt)
    nifty_map, latest_n = get_nifty_map(ndf, today, latest_smin)
    if latest_n is None:
        print("Nifty data unavailable (holiday/pre-open).")
        return

    sigs = []
    for sym in get_symbols():
        df = fetch_5m(fyers, f"NSE:{sym}-EQ", now_dt)
        if df is not None and not df.empty:
            s = find_signal(sym, df, today, latest_smin, nifty_map)
            if s is not None:
                sigs.append(s)
        time.sleep(0.18)

    sigs.sort(key=lambda x: (x["scan_min"], -x["rvol"]))
    state = load_state(str(today))
    sent = set(state["sent"])

    fresh = 0
    for idx, s in enumerate(sigs[:MAX_SIGNALS], 1):
        if s["smin"] == latest_smin and s["sym"] not in sent:
            msg = format_alert(s, idx)
            print("\n" + msg)
            send_telegram(msg)
            sent.add(s["sym"])
            fresh += 1

    state["sent"] = sorted(sent)
    save_state(state)
    print(f"Done. Nifty: {latest_n:+.2f}% | Today valid: {len(sigs)} | Sent: {fresh}")


def main():
    from fyers_apiv3 import fyersModel

    now = datetime.now(IST)
    fyers = fyersModel.FyersModel(
        client_id=os.environ["FYERS_APP_ID"].strip(),
        token=get_fyers_token(now.strftime("%Y-%m-%d")),
        log_path="",
    )

    if RUN_MODE != "loop" and "--loop" not in sys.argv:
        run_once(fyers, now)
        return

    print("VWAP C SHORT scanner started (LOOP mode, 09:25-12:00 IST)...")
    while True:
        now = datetime.now(IST)
        cur_min = now.hour * 60 + now.minute
        if cur_min > END_MIN + 2:
            print("12:00 IST done. Exiting.")
            break
        if cur_min >= START_MIN:
            try:
                run_once(fyers, now)
            except Exception as e:
                print(f"Cycle error (retrying next bar): {e}")
        now = datetime.now(IST)
        sleep_s = max(5, 300 - ((now.minute % 5) * 60 + now.second) + 8)
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
      
