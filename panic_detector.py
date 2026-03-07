import os
import re
import time
import requests
import yfinance as yf
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

# ================================
# CONFIG
# ================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ET = ZoneInfo("America/New_York")

MAIN_LOOP_SECONDS = 60
PANIC_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60

BITCOIN_MOVE_ALERT_THRESHOLD = 2.0

STOCK_TICKERS = [
    "NVDA","META","GOOGL","MSFT","AVGO","AMD","TSM","SOFI","INTC",
    "VOO","VTI","VXUS"
]

FUTURES_TICKERS = [
    "YM=F",
    "NQ=F",
    "ES=F"
]

BITCOIN_TICKER = "BTC-USD"
VIX_TICKER = "^VIX"
OIL_TICKER = "CL=F"
BOND_TICKER = "^TNX"

# ================================
# STATE
# ================================

last_panic_alert_time = 0
last_update_id = None

last_regime = None

last_stock_hour_sent = None
last_futures_slot_sent = None

last_btc_send_time = None
last_btc_price = None

last_vix_price = None
last_vix_bucket = None
last_vix_alert_signature = None

# ================================
# TELEGRAM
# ================================

def send_telegram_message(text):

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=20
        )
        r.raise_for_status()
        print("Telegram:", text)
        return True

    except Exception as e:
        print("Telegram error:", e)
        return False


# ================================
# SAFE REQUEST
# ================================

def safe_request(url):

    for _ in range(3):

        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return r

        except Exception as e:

            print("Request retry:", e)
            time.sleep(2)

    raise Exception("Request failed")


# ================================
# FEAR GREED
# ================================

def get_fear_greed():

    # 1️⃣ alternative.me
    try:

        r = safe_request("https://api.alternative.me/fng/")
        data = r.json()

        item = data["data"][0]

        return {
            "value": float(item["value"]),
            "description": item["value_classification"].lower(),
            "source": "alternative.me"
        }

    except Exception as e:
        print("Alt FearGreed failed:", e)


    # 2️⃣ CNN
    try:

        r = safe_request(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        )

        data = r.json()

        block = data.get("fear_and_greed", {})

        score = block.get("score")
        rating = block.get("rating")

        if score:

            return {
                "value": float(score),
                "description": rating.lower(),
                "source": "cnn"
            }

    except Exception as e:
        print("CNN FearGreed failed:", e)


    # 3️⃣ fallback scrape

    try:

        url = "https://www.finhacker.cz/en/fear-and-greed-index-historical-data-and-chart/"

        r = safe_request(url)

        text = re.sub(r"\s+", " ", r.text)

        match = re.search(
            r"current value.*?is\s+(\d+)\s*[-–]\s*"
            r"(extreme fear|fear|neutral|greed|extreme greed)",
            text,
            re.IGNORECASE,
        )

        if match:

            return {
                "value": float(match.group(1)),
                "description": match.group(2).lower(),
                "source": "finhacker",
            }

    except Exception as e:
        print("Finhacker failed:", e)

    return None


# ================================
# MARKET DATA
# ================================

def get_last_price_and_change(ticker):

    stock = yf.Ticker(ticker)

    hist = stock.history(period="5d")

    closes = hist["Close"].dropna()

    latest = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])

    pct = (latest - prev) / prev * 100

    return latest, pct


def get_market_data():

    vix_price, vix_change = get_last_price_and_change(VIX_TICKER)

    sp = yf.Ticker("^GSPC")

    hist = sp.history(period="1y")

    current = float(hist["Close"].iloc[-1])
    peak = float(hist["Close"].max())

    drawdown = (current - peak) / peak * 100

    fear_greed = get_fear_greed()

    return {
        "vix_price": vix_price,
        "vix_change": vix_change,
        "sp_current": current,
        "sp_peak": peak,
        "drawdown": drawdown,
        "fear_greed": fear_greed
    }


# ================================
# REGIME DETECTION
# ================================

def detect_market_regime(data):

    vix = data["vix_price"]
    drawdown = data["drawdown"]

    fg = None

    if data["fear_greed"]:
        fg = data["fear_greed"]["value"]

    if vix >= 35 and drawdown <= -12:
        return "CRISIS"

    if vix >= 25 or (fg and fg < 30):
        return "RISK OFF"

    if vix < 18 and (fg and fg > 60):
        return "RISK ON"

    return "NEUTRAL"


def maybe_send_regime_alert(data):

    global last_regime

    regime = detect_market_regime(data)

    if regime != last_regime:

        msg = (
            "🌎 MARKET REGIME SHIFT\n\n"
            f"Regime: {regime}\n"
            f"VIX: {data['vix_price']:.2f}\n"
            f"Drawdown: {data['drawdown']:.2f}%"
        )

        if data["fear_greed"]:

            fg = round(data["fear_greed"]["value"])
            desc = data["fear_greed"]["description"]

            msg += f"\nFear & Greed: {fg} ({desc})"

        send_telegram_message(msg)

        last_regime = regime


# ================================
# PANIC DETECTOR
# ================================

def panic_signal_triggered(data):

    fg = None

    if data["fear_greed"]:
        fg = data["fear_greed"]["value"]

    return (
        data["vix_price"] > 30
        and data["drawdown"] < -10
        and fg is not None
        and fg < 30
    )


def run_panic_check():

    global last_panic_alert_time

    data = get_market_data()

    if panic_signal_triggered(data):

        now = time.time()

        if now - last_panic_alert_time > PANIC_ALERT_COOLDOWN_SECONDS:

            fg = round(data["fear_greed"]["value"])

            msg = (
                "🚨 STOCK PANIC ALERT 🚨\n\n"
                f"VIX: {data['vix_price']:.2f}\n"
                f"Fear & Greed: {fg}\n"
                f"Drawdown: {data['drawdown']:.2f}%"
            )

            send_telegram_message(msg)

            last_panic_alert_time = now

    return data


# ================================
# BUY ZONE
# ================================

def check_buy_zone(data):

    if data["drawdown"] < -8 and data["vix_price"] > 25:

        send_telegram_message(
            "🟢 BUY ZONE DETECTED\n\n"
            f"Drawdown: {data['drawdown']:.2f}%\n"
            f"VIX: {data['vix_price']:.2f}"
        )


# ================================
# OIL SPIKE
# ================================

def check_oil_spike():

    try:

        price, pct = get_last_price_and_change(OIL_TICKER)

        if pct > 5:

            send_telegram_message(
                "⚠️ OIL SPIKE\n"
                f"Crude: {price:.2f}\n"
                f"Move: {pct:+.2f}%"
            )

    except:
        pass


# ================================
# BOND SPIKE
# ================================

def check_bond_spike():

    try:

        price, pct = get_last_price_and_change(BOND_TICKER)

        if abs(pct) > 3:

            send_telegram_message(
                "🏦 BOND YIELD MOVE\n"
                f"10Y: {price:.2f}\n"
                f"Move: {pct:+.2f}%"
            )

    except:
        pass


# ================================
# MAIN LOOP
# ================================

def main():

    send_telegram_message("✅ Brian Market Bot Started")

    while True:

        try:

            data = run_panic_check()

            maybe_send_regime_alert(data)

            check_buy_zone(data)

            check_oil_spike()

            check_bond_spike()

            time.sleep(MAIN_LOOP_SECONDS)

        except Exception as e:

            print("Loop error:", e)

            time.sleep(30)


if __name__ == "__main__":
    main()
