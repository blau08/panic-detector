import os
import time
import requests
import yfinance as yf

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL_SECONDS = 3600          # automatic market check every hour
TELEGRAM_POLL_SECONDS = 20             # bot checks for new Telegram commands every 20 sec
PANIC_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60

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
    "INTC",
]

last_panic_alert_time = 0
last_update_id = None
last_scheduled_check = 0


def require_env():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        raise ValueError("Missing TELEGRAM_CHAT_ID")


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            },
            timeout=20,
        )
        print("Telegram send status:", r.status_code)
        print("Telegram send response:", r.text)
        r.raise_for_status()
        return True
    except Exception as e:
        print("Telegram send error:", e)
        return False


def get_telegram_updates(offset=None, timeout=15):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "timeout": timeout,
    }
    if offset is not None:
        params["offset"] = offset

    try:
        r = requests.get(url, params=params, timeout=timeout + 10)
        r.raise_for_status()
        data = r.json()
        return data.get("result", [])
    except Exception as e:
        print("Telegram getUpdates error:", e)
        return []


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

    return {
        "vix_price": vix_price,
        "fear_greed": fear_greed,
        "sp_current": current,
        "sp_peak": peak,
        "drawdown": drawdown,
    }


def get_watchlist_data():
    rows = []

    for ticker in WATCHLIST:
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="5d", interval="1d")

            if hist.empty or hist["Close"].dropna().empty:
                rows.append(f"{ticker}: no data")
                continue

            closes = hist["Close"].dropna()
            latest = float(closes.iloc[-1])

            if len(closes) >= 2:
                prev = float(closes.iloc[-2])
                pct_change = ((latest - prev) / prev) * 100
                rows.append(f"{ticker}: {latest:.2f} ({pct_change:+.2f}%)")
            else:
                rows.append(f"{ticker}: {latest:.2f}")

        except Exception as e:
            print(f"Watchlist error for {ticker}: {e}")
            rows.append(f"{ticker}: error")

    return rows


def format_market_status():
    data = get_market_data()
    fear_greed_display = data["fear_greed"] if data["fear_greed"] is not None else "N/A"

    return (
        "📊 Market Status\n"
        f"VIX: {data['vix_price']:.2f}\n"
        f"Fear & Greed: {fear_greed_display}\n"
        f"S&P 500: {data['sp_current']:.2f}\n"
        f"1Y Peak: {data['sp_peak']:.2f}\n"
        f"Drawdown: {data['drawdown']:.2f}%"
    )


def format_watchlist_status():
    rows = get_watchlist_data()
    return "📈 Watchlist\n" + "\n".join(rows)


def panic_signal_triggered(market_data):
    return (
        market_data["fear_greed"] is not None
        and market_data["vix_price"] > 30
        and market_data["fear_greed"] < 20
        and market_data["drawdown"] < -20
    )


def run_panic_check(send_normal_status=False):
    global last_panic_alert_time

    print("Running panic check...")

    market_data = get_market_data()

    print("VIX:", market_data["vix_price"])
    print("Fear & Greed:", market_data["fear_greed"])
    print("Drawdown:", market_data["drawdown"])

    if send_normal_status:
        fear_greed_display = (
            market_data["fear_greed"]
            if market_data["fear_greed"] is not None
            else "N/A"
        )
        send_telegram_message(
            "📊 Scheduled Market Check\n"
            f"VIX: {market_data['vix_price']:.2f}\n"
            f"Fear & Greed: {fear_greed_display}\n"
            f"S&P 500: {market_data['sp_current']:.2f}\n"
            f"1Y Peak: {market_data['sp_peak']:.2f}\n"
            f"Drawdown: {market_data['drawdown']:.2f}%"
        )

    if panic_signal_triggered(market_data):
        now = time.time()

        if now - last_panic_alert_time > PANIC_ALERT_COOLDOWN_SECONDS:
            msg = (
                "🚨 PANIC BUY SIGNAL 🚨\n"
                f"VIX: {market_data['vix_price']:.2f}\n"
                f"Fear & Greed: {market_data['fear_greed']}\n"
                f"S&P 500: {market_data['sp_current']:.2f}\n"
                f"Drawdown: {market_data['drawdown']:.2f}%"
            )
            if send_telegram_message(msg):
                last_panic_alert_time = now
        else:
            print("Panic signal triggered, but cooldown active.")

    return market_data


def handle_command(text):
    cmd = text.strip().lower()

    if cmd == "/start":
        send_telegram_message(
            "✅ Brian Panic Detector is live.\n\n"
            "Commands:\n"
            "/status - market + watchlist\n"
            "/watchlist - watchlist only\n"
            "/panic - run panic check now\n"
            "/help - show commands"
        )

    elif cmd == "/help":
        send_telegram_message(
            "Commands:\n"
            "/status - market + watchlist\n"
            "/watchlist - watchlist only\n"
            "/panic - run panic check now\n"
            "/help - show commands"
        )

    elif cmd == "/status":
        try:
            market = format_market_status()
            watchlist = format_watchlist_status()
            send_telegram_message(f"{market}\n\n{watchlist}")
        except Exception as e:
            print("Status command error:", e)
            send_telegram_message(f"⚠️ /status failed: {e}")

    elif cmd == "/watchlist":
        try:
            send_telegram_message(format_watchlist_status())
        except Exception as e:
            print("Watchlist command error:", e)
            send_telegram_message(f"⚠️ /watchlist failed: {e}")

    elif cmd == "/panic":
        try:
            market_data = run_panic_check(send_normal_status=False)
            fear_greed_display = (
                market_data["fear_greed"]
                if market_data["fear_greed"] is not None
                else "N/A"
            )
            triggered = panic_signal_triggered(market_data)

            send_telegram_message(
                "🧪 Panic Check Complete\n"
                f"VIX: {market_data['vix_price']:.2f}\n"
                f"Fear & Greed: {fear_greed_display}\n"
                f"Drawdown: {market_data['drawdown']:.2f}%\n"
                f"Signal: {'ON' if triggered else 'OFF'}"
            )
        except Exception as e:
            print("Panic command error:", e)
            send_telegram_message(f"⚠️ /panic failed: {e}")

    else:
        send_telegram_message(
            "Unknown command.\nUse /help to see available commands."
        )


def process_telegram_updates():
    global last_update_id

    offset = last_update_id + 1 if last_update_id is not None else None
    updates = get_telegram_updates(offset=offset, timeout=15)

    for update in updates:
        try:
            last_update_id = update["update_id"]

            message = update.get("message", {})
            chat = message.get("chat", {})
            text = message.get("text", "")

            if not text:
                continue

            incoming_chat_id = str(chat.get("id"))
            if incoming_chat_id != str(TELEGRAM_CHAT_ID):
                print(f"Ignoring unauthorized chat_id: {incoming_chat_id}")
                continue

            print("Received command:", text)
            handle_command(text)

        except Exception as e:
            print("Update processing error:", e)


def main():
    global last_scheduled_check

    require_env()

    send_telegram_message(
        "✅ Bot started on Railway\n"
        "Use /status, /watchlist, /panic, or /help"
    )

    try:
        run_panic_check(send_normal_status=True)
        last_scheduled_check = time.time()
    except Exception as e:
        print("Startup market check error:", e)
        send_telegram_message(f"⚠️ Startup market check failed: {e}")

    while True:
        try:
            process_telegram_updates()

            now = time.time()
            if now - last_scheduled_check >= CHECK_INTERVAL_SECONDS:
                try:
                    run_panic_check(send_normal_status=True)
                    last_scheduled_check = now
                except Exception as e:
                    print("Scheduled check error:", e)
                    send_telegram_message(f"⚠️ Scheduled check failed: {e}")

            time.sleep(TELEGRAM_POLL_SECONDS)

        except Exception as e:
            print("Main loop error:", e)
            send_telegram_message(f"⚠️ Main loop error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
