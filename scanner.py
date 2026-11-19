import os
import requests
from datetime import datetime, timezone, timedelta

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
EVENT_NAME = os.getenv("GITHUB_EVENT_NAME", "schedule")
STATE_FILE = "alerted_stocks.txt"

TV_SCAN_URL = "https://scanner.tradingview.com/india/scan"

FO_SYMBOLS = {
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL", "LT",
    "ITC", "AXISBANK", "KOTAKBANK", "TATAMOTORS", "M_M", "BAJFINANCE", "SUNPHARMA",
    "MARUTI", "TRENT", "HAL", "BEL", "TATASTEEL", "HINDALCO", "JSWSTEEL", "ADANIENT",
    "ADANIPORTS", "COALINDIA", "NTPC", "POWERGRID", "ONGC", "BPCL", "TITAN",
    "ULTRACEMCO", "GRASIM", "TECHM", "WIPRO", "HCLTECH", "DRREDDY", "CIPLA",
    "DIVISLAB", "BAJAJ_AUTO", "EICHERMOT", "HEROMOTOCO", "INDUSINDBK", "BANKBARODA",
    "PNB", "CHOLAFIN", "PFC", "RECLTD", "DLF", "SIEMENS", "VEDL", "ZOMATO", "JIOFIN"
}

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
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)

def fetch_tradingview_intraday():
    """5-Minute Intraday VWAP (VWAP|5) aur 5m Candle Open/Close fetch karta hai."""
    payload = {
        "filter": [
            {"left": "exchange", "operation": "equal", "right": "NSE"},
            {"left": "volume", "operation": "greater", "right": 500000}
        ],
        "options": {"lang": "en"},
        "markets": ["india"],
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "columns": [
            "name",                     # 0: Symbol
            "close",                    # 1: Live LTP
            "change",                   # 2: Day % Change
            "VWAP|5",                   # 3: TRUE 5-Minute Intraday VWAP
            "relative_volume_10d_calc", # 4: RVOL
            "high",                     # 5: Day High
            "low",                      # 6: Day Low
            "open",                     # 7: Day Open
            "open|5",                   # 8: Current 5m Candle Open
            "high|5",                   # 9: Current 5m Candle High
            "low|5"                     # 10: Current 5m Candle Low
        ],
        "sort": {"sortBy": "relative_volume_10d_calc", "sortOrder": "desc"},
        "range": [0, 300]
    }
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    res = requests.post(TV_SCAN_URL, json=payload, headers=headers, timeout=15)
    if res.status_code == 200:
        return res.json().get("data", [])
    return []
def main():
    ist_now = get_ist_now()
    today_str = ist_now.strftime("%Y-%m-%d")
    time_str = ist_now.strftime("%H:%M")

    # Strict Time Guard: Sirf 09:25 AM se 02:15 PM IST ke beech hi chalega
    if not ("09:25" <= time_str <= "14:15"):
        print(f"[{time_str} IST] Outside Intraday Window (09:25 - 14:15). Skipping.")
        return

    alerted = load_state(today_str)
    rows = fetch_tradingview_intraday()

    new_alerts = []

    for item in rows:
        d = item.get("d", [])
        if len(d) < 11:
            continue
        sym, cmp_p, chg, vwap_5m, rvol, day_h, day_l, day_o, open_5m, high_5m, low_5m = d

        if sym not in FO_SYMBOLS or not cmp_p or not vwap_5m or not rvol or not open_5m:
            continue

        cmp_p = round(float(cmp_p), 2)
        vwap_5m = round(float(vwap_5m), 2)
        chg = round(float(chg), 2)
        rvol = round(float(rvol), 2)
        open_5m = float(open_5m)
        low_5m = float(low_5m)
        high_5m = float(high_5m)

        if rvol < 1.3:
            continue

        vwap_dist_pct = ((cmp_p - vwap_5m) / vwap_5m) * 100

        # BULLISH PULLBACK + GREEN 5M BOUNCE CONFIRMATION
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
                alerted.add(key)
                sl = round(min(low_5m, vwap_5m) * 0.997, 2)
                risk = max(round(cmp_p - sl, 2), 0.5)
                tgt = round(cmp_p + (risk * 2.5), 2)
                new_alerts.append(
                    f"🟢 <b>LONG (5m VWAP Bounce): {sym}</b>\n"
                    f"   CMP: ₹{cmp_p} (+{chg}%) | RVOL: <b>{rvol}x</b>\n"
                    f"   5m VWAP: ₹{vwap_5m} | 5m Candle: Green Bounce\n"
                    f"   SL: ₹{sl} | Target (2.5R): ₹{tgt}"
                )

        # BEARISH REJECTION + RED 5M CANDLE CONFIRMATION
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
                alerted.add(key)
                sl = round(max(high_5m, vwap_5m) * 1.003, 2)
                risk = max(round(sl - cmp_p, 2), 0.5)
                tgt = round(cmp_p - (risk * 2.5), 2)
                new_alerts.append(
                    f"🔴 <b>SHORT (5m VWAP Rejection): {sym}</b>\n"
                    f"   CMP: ₹{cmp_p} ({chg}%) | RVOL: <b>{rvol}x</b>\n"
                    f"   5m VWAP: ₹{vwap_5m} | 5m Candle: Red Reject\n"
                    f"   SL: ₹{sl} | Target (2.5R): ₹{tgt}"
                )

    if new_alerts:
        header = f"⚡ <b>CONFIRMED 5m VWAP BOUNCE ({time_str} IST)</b> ⚡\n\n"
        send_telegram(header + "\n\n".join(new_alerts))

    save_state(today_str, alerted)
    
