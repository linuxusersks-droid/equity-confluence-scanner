import os
import requests
from datetime import datetime, timezone, timedelta

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
EVENT_NAME = os.getenv("GITHUB_EVENT_NAME", "schedule") # Pata lagane ke liye ki manual test hai ya cron
STATE_FILE = "alerted_stocks.txt"

# TradingView Official Real-Time India Screener Endpoint (Zero Delay, No Key Needed)
TV_SCAN_URL = "https://scanner.tradingview.com/india/scan"

# Liquid F&O Watchlist
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
        print("❌ ERROR: TELEGRAM_BOT_TOKEN ya TELEGRAM_CHAT_ID Secret missing hai!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    print(f"Telegram API Status: {res.status_code}")

def fetch_tradingview_realtime():
    """TradingView se 1 single call mein Real-Time Price, VWAP, RVOL aur OHLC laata hai."""
    payload = {
        "filter": [
            {"left": "exchange", "operation": "equal", "right": "NSE"},
            {"left": "volume", "operation": "greater", "right": 500000}
        ],
        "options": {"lang": "en"},
        "markets": ["india"],
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "columns": [
            "name",               # 0: Symbol
            "close",              # 1: Real-Time LTP
            "change",             # 2: % Change
            "VWAP",               # 3: Real-Time Intraday VWAP
            "relative_volume_10d_calc", # 4: Real-Time RVOL (10-Day Avg comparison)
            "high",               # 5: Today's High
            "low",                # 6: Today's Low
            "open"                # 7: Today's Open
        ],
        "sort": {"sortBy": "relative_volume_10d_calc", "sortOrder": "desc"},
        "range": [0, 300]
    }
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    res = requests.post(TV_SCAN_URL, json=payload, headers=headers, timeout=15)
    if res.status_code == 200:
        return res.json().get("data", [])
    print(f"TradingView HTTP Error: {res.status_code}")
    return []

def main():
    ist_now = get_ist_now()
    today_str = ist_now.strftime("%Y-%m-%d")
    time_str = ist_now.strftime("%H:%M")

    alerted = load_state(today_str)
    rows = fetch_tradingview_realtime()

    new_alerts = []
    top_rvol_preview = []

    for item in rows:
        d = item.get("d", [])
        if len(d) < 8:
            continue
        sym, cmp_p, chg, vwap, rvol, high_p, low_p, open_p = d

        if sym not in FO_SYMBOLS or not cmp_p or not vwap or not rvol:
            continue

        cmp_p = round(float(cmp_p), 2)
        vwap = round(float(vwap), 2)
        chg = round(float(chg), 2)
        rvol = round(float(rvol), 2)

        # Diagnostic preview ke liye top 3 RVOL stocks store karein
        if len(top_rvol_preview) < 3:
            top_rvol_preview.append(f"• <b>{sym}</b>: ₹{cmp_p} ({chg}%) | VWAP: ₹{vwap} | RVOL: {rvol}x")

        # CORE FILTER: High Relative Volume (RVOL >= 1.3x)
        if rvol < 1.3:
            continue

        # VWAP Distance % (Kitna paas hai price VWAP ke)
        vwap_dist_pct = ((cmp_p - vwap) / vwap) * 100

        # 1. BULLISH VWAP PULLBACK & HOLD:
        # Stock Strong Green hai (chg > 0.8%, Close > Open) + Price VWAP ke upar 0.05% se 0.6% zone mein hai
        if chg >= 0.8 and cmp_p > open_p and (0.02 <= vwap_dist_pct <= 0.65):
            key = f"BULL_{sym}"
            if key not in alerted:
                alerted.add(key)
                sl = round(vwap * 0.996, 2)
                risk = max(round(cmp_p - sl, 2), 0.5)
                tgt = round(cmp_p + (risk * 2.5), 2)
                new_alerts.append(
                    f"🟢 <b>LONG (VWAP Pullback): {sym}</b>\n"
                    f"   CMP: ₹{cmp_p} (+{chg}%) | RVOL: <b>{rvol}x</b>\n"
                    f"   Live VWAP: ₹{vwap} | Day High: ₹{high_p}\n"
                    f"   SL: ₹{sl} | Target (2.5R): ₹{tgt}"
                )

        # 2. BEARISH VWAP REJECTION:
        # Stock Weak Red hai (chg < -0.8%, Close < Open) + Price VWAP ke niche -0.05% se -0.65% zone mein hai
        elif chg <= -0.8 and cmp_p < open_p and (-0.65 <= vwap_dist_pct <= -0.02):
            key = f"BEAR_{sym}"
            if key not in alerted:
                alerted.add(key)
                sl = round(vwap * 1.004, 2)
                risk = max(round(sl - cmp_p, 2), 0.5)
                tgt = round(cmp_p - (risk * 2.5), 2)
                new_alerts.append(
                    f"🔴 <b>SHORT (VWAP Rejection): {sym}</b>\n"
                    f"   CMP: ₹{cmp_p} ({chg}%) | RVOL: <b>{rvol}x</b>\n"
                    f"   Live VWAP: ₹{vwap} | Day Low: ₹{low_p}\n"
                    f"   SL: ₹{sl} | Target (2.5R): ₹{tgt}"
                )

    if new_alerts:
        header = f"⚡ <b>REAL-TIME F&O VWAP CONFLUENCE ({time_str} IST)</b> ⚡\n<i>Source: TradingView Direct Feed (Zero Dhan Load)</i>\n\n"
        send_telegram(header + "\n\n".join(new_alerts))
    elif EVENT_NAME == "workflow_dispatch":
        # Agar aapne manually "Run workflow" dabaya hai, toh 100% Confirmation message aayega!
        preview_txt = "\n".join(top_rvol_preview) if top_rvol_preview else "No data"
        test_msg = (
            f"✅ <b>SCANNER DIAGNOSTIC OK ({time_str} IST)</b>\n"
            f"TradingView Real-Time Feed Connected! (F&O Scanned: {len(FO_SYMBOLS)})\n\n"
            f"<i>Abhi koi stock exact 0.6% VWAP pullback zone mein nahi hai, par yeh Top 3 High-RVOL stocks live chal rahe hain:</i>\n"
            f"{preview_txt}"
        )
        send_telegram(test_msg)
    else:
        print(f"[{time_str} IST] Scanned successfully. No new VWAP pullback setups right now.")

    save_state(today_str, alerted)

if __name__ == "__main__":
    main()
    
