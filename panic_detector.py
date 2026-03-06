import os
import time
import requests
import yfinance as yf

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
last_alert_time = 0


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env vars missing")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            },
            timeout=10,
        )
        r.raise_for_status()
        print("Telegram message sent")
        return True
    except Exception as e:
        print("Telegram send error:", e)
        return False


def get_fear_greed():
    url = "https://api.alternative.me/fng/?limit=1"

    try:
        response = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 panic-detector/1.0"},
        )
        response.raise_for_status()
        data = response.json()
        return int(data["data"][0]["value"])
    except Exception as e:
        print("Fear & Greed fetch error:", e)
        return None


def check_market():
    global last_alert_time

    print("Checking markets...")

    try:
        vix = yf.Ticker("^VIX")
        vix_hist = vix.history(period="5d", interval="1d")
        if vix_hist.empty:
            raise ValueError("No VIX data returned")
        vix_price = float(vix_hist["Close"].dropna().iloc[-1])

        sp = yf.Ticker("^GSPC")
        sp_data = sp.history(period="1y", interval="1d")
        if sp_data.empty:
            raise ValueError("No S&P 500 data returned")

        current = float(sp_data["Close"].dropna().iloc[-1])
        peak = float(sp_data["Close"].max())
        drawdown = (current - peak) / peak * 100

        fear_greed = get_fear_greed()

        print("VIX:", vix_price)
        print("Fear & Greed:", fear_greed)
        print("Drawdown:", drawdown)

        if fear_greed is not None:
            if vix_price > 30 and fear_greed < 20 and drawdown < -20:
                now = time.time()

                if now - last_alert_time > ALERT_COOLDOWN_SECONDS:
                    msg = (
                        "🚨 PANIC BUY SIGNAL 🚨\n"
                        f"VIX: {vix_price:.2f}\n"
                        f"Fear & Greed: {fear_greed}\n"
                        f"Drawdown: {drawdown:.2f}%"
                    )
                    if send_telegram_message(msg):
                        last_alert_time = now
                else:
                    print("Signal triggered but cooldown active.")

    except Exception as e:
        print("Market check error:", e)
        send_telegram_message(f"⚠️ Market check error: {e}")


if __name__ == "__main__":
    send_telegram_message("✅ Panic detector started on Railway")

    while True:
        check_market()
        print("Sleeping 1 hour...")
        time.sleep(3600)
