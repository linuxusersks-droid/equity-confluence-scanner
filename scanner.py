import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "alerted_stocks.txt"
CHARTINK_URL = "https://chartink.com/screener/process"

# 1. BULLISH CONFLUENCE: NR Compression + PDH Breakout + 1.5x RVOL + 5m VWAP Pullback
BULLISH_SCAN = (
    "( {33489} ( "
    "[-1] 1 day high - [-1] 1 day low < [-2] 1 day high - [-2] 1 day low and "
    "latest close > 1 day ago high and "
    "[0] 15 minute volume > [0] 15 minute sma( volume,20 ) * 1.5 and "
    "[0] 5 minute low <= [0] 5 minute vwap * 1.003 and "
    "[0] 5 minute close > [0] 5 minute vwap "
    ") )"
)

# 2. BEARISH CONFLUENCE: NR Compression + PDL Breakdown + 1.5x RVOL + 5m VWAP Rejection
BEARISH_SCAN = (
    "( {33489} ( "
    "[-1] 1 day high - [-1] 1 day low < [-2] 1 day high - [-2] 1 day low and "
    "latest close < 1 day ago low and "
    "[0] 15 minute volume > [0] 15 minute sma( volume,20 ) * 1.5 and "
    "[0] 5 minute high >= [0] 5 minute vwap * 0.997 and "
    "[0] 5 minute close < [0] 5 minute vwap "
    ") )"
)

def get_ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))

def load_state(today_str):
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]
    # Agar file purane din ki hai, toh reset kar do
    if not lines or lines[0] != today_str:
        return set()
    return set(lines[1:])

def save_state(today_str, alerted_set):
    with open(STATE_FILE, "w") as f:
        f.write(today_str + "\n")
        for item in sorted(alerted_set):
            f.write(item + "\n")

def fetch_chartink(scan_clause):
    with requests.Session() as s:
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        })
        r = s.get("https://chartink.com/screener/time-pass-48", timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        csrf = soup.find("meta", {"name": "csrf-token"})["content"]

        s.headers.update({"X-CSRF-Token": csrf, "X-Requested-With": "XMLHttpRequest"})
        res = s.post(CHARTINK_URL, data={"scan_clause": scan_clause}, timeout=15)
        if res.status_code == 200:
            return res.json().get("data", [])
        return []

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)

def main():
    ist_now = get_ist_now()
    today_str = ist_now.strftime("%Y-%m-%d")
    time_str = ist_now.strftime("%H:%M")

    alerted = load_state(today_str)
    bull_stocks = fetch_chartink(BULLISH_SCAN)
    bear_stocks = fetch_chartink(BEARISH_SCAN)

    new_alerts = []

    for st in bull_stocks:
        sym = st["nsecode"]
        key = f"BULL_{sym}"
        if key not in alerted:
            alerted.add(key)
            price = st["close"]
            chg = st["per_chg"]
            sl_est = round(price * 0.996, 1) # Approx 0.4% tight SL below VWAP
            new_alerts.append(
                f"🟢 <b>LONG (VWAP Pullback): {sym}</b>\n"
                f"   CMP: ₹{price} ({chg}%)\n"
                f"   Ref SL: ~₹{sl_est} (5m VWAP Low)"
            )

    for st in bear_stocks:
        sym = st["nsecode"]
        key = f"BEAR_{sym}"
        if key not in alerted:
            alerted.add(key)
            price = st["close"]
            chg = st["per_chg"]
            sl_est = round(price * 1.004, 1) # Approx 0.4% tight SL above VWAP
            new_alerts.append(
                f"🔴 <b>SHORT (VWAP Rejection): {sym}</b>\n"
                f"   CMP: ₹{price} ({chg}%)\n"
                f"   Ref SL: ~₹{sl_est} (5m VWAP High)"
            )

    if new_alerts:
        header = f"⚡ <b>F&O TRIPLE CONFLUENCE ({time_str} IST)</b> ⚡\n<i>NR Compression + RVOL + VWAP Pullback</i>\n\n"
        send_telegram(header + "\n\n".join(new_alerts))
        print(f"Sent {len(new_alerts)} new alerts.")
    else:
        print(f"[{time_str} IST] No new stocks matched.")

    save_state(today_str, alerted)

if __name__ == "__main__":
    main()
  
