import os
import time
import requests
import yfinance as yf

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL_SECONDS = 3600  # 1 hour
PANIC_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60  # 6 hours

WATCHLIST = [
    "NVDA",
    "META",
    "GOOGL",
    "MSFT",
    "AVGO",
    "AMD",
    "TSM",
    "SOFI",
    "INTC",
    "VOO",
    "VTI",
    "VXUS",
]

last_panic_alert_time = 0


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
            timeout=15,
        )
        print("Telegram status:", r.status_code)
        print("Telegram response:", r.text)
        r.raise_for_status()
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


def get_market_data():
    vix = yf.Ticker("^VIX")
    vix_hist = vix.history(period="5d", interval="1d")
    if vix_hist.empty or vix_hist["Close"].dropna().empty:
        raise ValueError("No VIX data returned")
    vix_price = float(vix_hist["Close"].dropna().iloc[-1])

    sp = yf.Ticker("^GSPC")
    sp_data = sp.history(period="1y", interval="1d")
    if sp_data.empty or sp_data["Close"].dropna().empty:
        raise ValueError("No S&P 500 data returned")

    current = float(sp_data["Close"].dropna().iloc[-1])
    peak = float(sp_data["Close"].max())
    drawdown = (current - peak) / peak * 100

    fear_greed = get_fear_greed()

    return vix_price, fear_greed, drawdown, current, peak


def get_watchlist_update():
    lines = ["📈 Watchlist Update"]

    for ticker in WATCHLIST:
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="5d", interval="1d")

            if hist.empty or len(hist["Close"].dropna()) == 0:
                lines.append(f"{ticker}: no data")
                continue

            closes = hist["Close"].dropna()
            latest = float(closes.iloc[-1])

            if len(closes) >= 2:
                prev = float(closes.iloc[-2])
                pct_change = ((latest - prev) / prev) * 100
                lines.append(f"{ticker}: {latest:.2f} ({pct_change:+.2f}%)")
            else:
                lines.append(f"{ticker}: {latest:.2f}")

        except Exception as e:
            print(f"Watchlist error for {ticker}: {e}")
            lines.append(f"{ticker}: error")

    return "\n".join(lines)


def check_market():
    global last_panic_alert_time

    print("Checking markets...")

    try:
        vix_price, fear_greed, drawdown, current, peak = get_market_data()

        print("VIX:", vix_price)
        print("Fear & Greed:", fear_greed)
        print("Drawdown:", drawdown)

        market_status = (
            "📊 Market Status\n"
            f"VIX: {vix_price:.2f}\n"
            f"Fear & Greed: {fear_greed if fear_greed is not None else 'N/A'}\n"
            f"S&P 500: {current:.2f}\n"
            f"1Y Peak: {peak:.2f}\n"
            f"Drawdown: {drawdown:.2f}%"
        )

        watchlist_status = get_watchlist_update()

        send_telegram_message(f"{market_status}\n\n{watchlist_status}")

        panic_signal = (
            fear_greed is not None
            and vix_price > 30
            and fear_greed < 20
            and drawdown < -20
        )

        if panic_signal:
            now = time.time()

            if now - last_panic_alert_time > PANIC_ALERT_COOLDOWN_SECONDS:
                panic_message = (
                    "🚨 PANIC BUY SIGNAL 🚨\n"
                    f"VIX: {vix_price:.2f}\n"
                    f"Fear & Greed: {fear_greed}\n"
                    f"S&P 500: {current:.2f}\n"
                    f"Drawdown: {drawdown:.2f}%"
                )
                if send_telegram_message(panic_message):
                    last_panic_alert_time = now
            else:
                print("Panic signal triggered, but cooldown active.")

    except Exception as e:
        print("Market check error:", e)
        send_telegram_message(f"⚠️ Market check error: {e}")


if __name__ == "__main__":
    send_telegram_message("✅ Watchlist + panic detector started on Railway")
    check_market()

    while True:
        print(f"Sleeping {CHECK_INTERVAL_SECONDS} seconds...")
        time.sleep(CHECK_INTERVAL_SECONDS)
        check_market()
