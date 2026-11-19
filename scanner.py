import os
import requests
from datetime import datetime, timezone, timedelta

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "alerted_stocks.txt"

TV_SCAN_URL = "https://scanner.tradingview.com/india/scan"

def get_ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))

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
            timeout=10
        )
        print(f"Telegram status: {res.status_code}")
    except requests.RequestException as e:
        print(f"Telegram send error: {e}")

def fetch_tradingview_intraday():
    """Poore NSE Liquid & F&O Universe (Market Cap >= 5,000 Cr, Price >= 100) ka 5m VWAP data laata hai."""
    payload = {
        "filter": [
            {"left": "exchange", "operation": "equal", "right": "NSE"},
            {"left": "type", "operation": "equal", "right": "stock"},
            {"left": "market_cap_basic", "operation": "greater", "right": 50000000000}, # >= 5,000 Cr (All F&O + Liquid)
            {"left": "close", "operation": "greater", "right": 100},                    # No penny stocks
            {"left": "volume", "operation": "greater", "right": 50000}                  # Works from 9:25 AM
        ],
        "options": {"lang": "en"},
        "markets": ["india"],
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "columns": [
            "name",                     # 0: Symbol
            "close",                    # 1: Live LTP
            "change",                   # 2: Day % Change
            "VWAP|5",                   # 3: True 5m Intraday VWAP
            "relative_volume_10d_calc", # 4: Cumulative 10d RVOL
            "high",                     # 5: Day High
            "low",                      # 6: Day Low
            "open",                     # 7: Day Open
            "open|5",                   # 8: Current 5m Open
            "high|5",                   # 9: Current 5m High
            "low|5"                     # 10: Current 5m Low
        ],
        "sort": {"sortBy": "relative_volume_10d_calc", "sortOrder": "desc"},
        "range": [0, 300]
    }
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    try:
        res = requests.post(TV_SCAN_URL, json=payload, headers=headers, timeout=15)
        if res.status_code == 200:
            return res.json().get("data", [])
        print(f"TradingView HTTP Error: {res.status_code}")
    except requests.RequestException as e:
        print(f"TradingView request error: {e}")
    return []

def main():
    ist_now = get_ist_now()

    # 1. Weekend Guard (Saturday=5, Sunday=6 -> Skip)
    if ist_now.weekday() >= 5:
        print("Weekend detected. Skipping scan.")
        return

    today_str = ist_now.strftime("%Y-%m-%d")
    time_str = ist_now.strftime("%H:%M")

    # 2. Intraday Time Window Guard (09:25 AM to 02:15 PM IST)
    if not ("09:25" <= time_str <= "14:15"):
        print(f"[{time_str} IST] Outside Intraday Window (09:25 - 14:15). Skipping.")
        return

    # 3. Time-Adjusted Projected RVOL Factor (09:15 = 555 min, 15:30 = 930 min -> 375 min total)
    now_mins = ist_now.hour * 60 + ist_now.minute
    elapsed_fraction = max(min((now_mins - 555) / 375.0, 1.0), 0.04)

    alerted = load_state(today_str)
    rows = fetch_tradingview_intraday()
    if not rows:
        print(f"[{time_str} IST] No data received from TradingView.")
        return

    candidates = []

    for item in rows:
        d = item.get("d", [])
        if len(d) < 11:
            continue

        # Strict NoneType Guard
        if any(x is None for x in d[:11]):
            continue

        sym, cmp_p, chg, vwap_5m, rvol_raw, day_h, day_l, day_o, open_5m, high_5m, low_5m = d

        cmp_p = round(float(cmp_p), 2)
        vwap_5m = round(float(vwap_5m), 2)
        chg = round(float(chg), 2)
        rvol_raw = float(rvol_raw)
        day_h = float(day_h)
        day_l = float(day_l)
        day_o = float(day_o)
        open_5m = float(open_5m)
        high_5m = float(high_5m)
        low_5m = float(low_5m)

        if vwap_5m <= 0 or cmp_p <= 0:
            continue

        # Projected Full-Day RVOL (Subah 9:25 par bhi exact volume surge pakdega)
        proj_rvol = round(rvol_raw / elapsed_fraction, 2)
        if proj_rvol < 1.5:
            continue

        vwap_dist_pct = ((cmp_p - vwap_5m) / vwap_5m) * 100

        # BULLISH 5m VWAP PULLBACK + GREEN BOUNCE CONFIRMATION
        if (
            chg >= 1.0
            and cmp_p > day_o
            and (0.02 <= vwap_dist_pct <= 0.45)
            and low_5m <= (vwap_5m * 1.003)
            and cmp_p > open_5m
            and cmp_p < (day_h * 0.997)
        ):
            key = f"BULL_{sym}"
            if key not in alerted:
                sl = round(min(low_5m, vwap_5m) * 0.997, 2)
                risk = max(round(cmp_p - sl, 2), 0.5)
                tgt = round(cmp_p + (risk * 2.5), 2)
                msg = (
                    f"🟢 <b>LONG (5m VWAP Bounce): {sym}</b>\n"
                    f"   CMP: ₹{cmp_p} (+{chg}%) | Proj RVOL: <b>{proj_rvol}x</b>\n"
                    f"   5m VWAP: ₹{vwap_5m} | 5m Candle: Green Bounce\n"
                    f"   SL: ₹{sl} | Target (2.5R): ₹{tgt}"
                )
                candidates.append((proj_rvol, key, msg))

        # BEARISH 5m VWAP REJECTION + RED CANDLE CONFIRMATION
        elif (
            chg <= -1.0
            and cmp_p < day_o
            and (-0.45 <= vwap_dist_pct <= -0.02)
            and high_5m >= (vwap_5m * 0.997)
            and cmp_p < open_5m
            and cmp_p > (day_l * 1.003)
        ):
            key = f"BEAR_{sym}"
            if key not in alerted:
                sl = round(max(high_5m, vwap_5m) * 1.003, 2)
                risk = max(round(sl - cmp_p, 2), 0.5)
                tgt = round(cmp_p - (risk * 2.5), 2)
                msg = (
                    f"🔴 <b>SHORT (5m VWAP Rejection): {sym}</b>\n"
                    f"   CMP: ₹{cmp_p} ({chg}%) | Proj RVOL: <b>{proj_rvol}x</b>\n"
                    f"   5m VWAP: ₹{vwap_5m} | 5m Candle: Red Reject\n"
                    f"   SL: ₹{sl} | Target (2.5R): ₹{tgt}"
                )
                candidates.append((proj_rvol, key, msg))

    if candidates:
        # Highest RVOL ke hisaab se Top 5 setups hi bhejein (No Spam)
        candidates.sort(key=lambda x: x[0], reverse=True)
        top_alerts = candidates[:5]
        alert_texts = []
        for _, key, msg in top_alerts:
            alerted.add(key)
            alert_texts.append(msg)

        header = f"⚡ <b>CONFIRMED 5m VWAP BOUNCE ({time_str} IST)</b> ⚡\n\n"
        send_telegram(header + "\n\n".join(alert_texts))
        print(f"[{time_str} IST] Dispatched {len(top_alerts)} alerts.")
    else:
        print(f"[{time_str} IST] Scanned {len(rows)} stocks. No confirmed bounce setup.")

    save_state(today_str, alerted)

if __name__ == "__main__":
    main()
