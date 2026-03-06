import os
import time
import requests
import yfinance as yf

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env vars missing")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text
            },
            timeout=10
        )
        print("Telegram status:", r.status_code, r.text)
    except Exception as e:
        print("Telegram send error:", e)


def get_fear_greed():
    url = "https://api.alternative.me/fng/?limit=1"
    try:
        response = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        response.raise_for_status()
        data = response.json()
        return int(data["data"][0]["value"])
    except Exception as e:
        print("Fear & Greed fetch error:", e)
        return None


def check_market():
    print("Checking markets...")

    try:
        vix = yf.Ticker("^VIX")
        vix_price = vix.history(period="1d")["Close"].iloc[-1]

        sp = yf.Ticker("^GSPC")
        sp_data = sp.history(period="1y")

        current = sp_data["Close"].iloc[-1]
        peak = sp_data["Close"].max()
        drawdown = (current - peak) / peak * 100

        fear_greed = get_fear_greed()

        print("VIX:", vix_price)
        print("Fear & Greed:", fear_greed)
        print("Drawdown:", drawdown)

        if fear_greed is not None:
            if vix_price > 30 and fear_greed < 20 and drawdown < -20:
                msg = (
                    "🚨 PANIC BUY SIGNAL 🚨\n"
                    f"VIX: {vix_price:.2f}\n"
                    f"Fear & Greed: {fear_greed}\n"
                    f"Drawdown: {drawdown:.2f}%"
                )
                send_telegram_message(msg)

    except Exception as e:
        print("Market check error:", e)
        send_telegram_message(f"⚠️ Market check error: {e}")


send_telegram_message("✅ Railway bot started")

while True:
    check_market()
    print("Sleeping 1 hour...")
    time.sleep(3600)
