import os
import time
import requests
import yfinance as yf
import fear_and_greed

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL_SECONDS = 3600
TELEGRAM_POLL_SECONDS = 20
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
    "BTC-USD",
    "YM=F",   # Dow futures
    "NQ=F",   # Nasdaq futures
    "ES=F",   # S&P 500 futures
]

LABEL_MAP = {
    "NVDA": "NVDA",
    "META": "META",
    "GOOGL": "GOOGL",
    "MSFT": "MSFT",
    "AVGO": "AVGO",
    "AMD": "AMD",
    "TSM": "TSM",
    "SOFI": "SOFI",
    "INTC": "INTC",
    "VOO": "VOO",
    "VTI": "VTI",
    "VXUS": "VXUS",
    "BTC-USD": "Bitcoin",
    "YM=F": "Dow Futures",
    "NQ=F": "Nasdaq Futures",
    "ES=F": "S&P Futures",
}

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
    params = {"timeout": timeout}

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


def get_cnn_fear_greed():
    try:
        fg = fear_and_greed.get()
        return {
            "value": float(fg.value),
            "description": str(fg.description),
            "last_update": str(fg.last_update),
        }
    except Exception as e:
        print("CNN Fear & Greed fetch error:", e)
        return None


def get_last_price_and_change(ticker):
    stock = yf.Ticker(ticker)
    hist = stock.history(period="5d", interval="1d")

    if hist.empty or hist["Close"].dropna().empty:
        raise ValueError(f"No data returned for {ticker}")

    closes = hist["Close"].dropna()
    latest = float(closes.iloc[-1])

    if len(closes) >= 2:
        prev = float(closes.iloc[-2])
        pct_change = ((latest - prev) / prev) * 100
    else:
        pct_change = 0.0

    return latest, pct_change


def get_market_data():
    vix_price, vix_change = get_last_price_and_change("^VIX")

    sp = yf.Ticker("^GSPC")
    sp_hist = sp.history(period="1y", interval="1d")
    if sp_hist.empty or sp_hist["Close"].dropna().empty:
        raise ValueError("No S&P 500 history returned")

    sp_current = float(sp_hist["Close"].dropna().iloc[-1])
    sp_peak = float(sp_hist["Close"].max())
    drawdown = (sp_current - sp_peak) / sp_peak * 100

    cnn_fg = get_cnn_fear_greed()

    return {
        "vix_price": vix_price,
        "vix_change": vix_change,
        "sp_current": sp_current,
        "sp_peak": sp_peak,
        "drawdown": drawdown,
        "cnn_fg": cnn_fg,
    }


def format_watchlist_status():
    lines = ["📈 Watchlist"]

    for ticker in WATCHLIST:
        label = LABEL_MAP.get(ticker, ticker)

        try:
            price, pct = get_last_price_and_change(ticker)
            lines.append(f"{label}: {price:.2f} ({pct:+.2f}%)")
        except Exception as e:
            print(f"Watchlist error for {ticker}: {e}")
            lines.append(f"{label}: error")

    return "\n".join(lines)


def format_market_status():
    data = get_market_data()

    if data["cnn_fg"] is not None:
        fg_value = round(data["cnn_fg"]["value"])
        fg_desc = data["cnn_fg"]["description"]
    else:
        fg_value = "N/A"
        fg_desc = "unavailable"

    return (
        "📊 Market Status\n"
        f"VIX: {data['vix_price']:.2f} ({data['vix_change']:+.2f}%)\n"
        f"CNN Fear & Greed: {fg_value} ({fg_desc})\n"
        f"S&P 500: {data['sp_current']:.2f}\n"
        f"1Y Peak: {data['sp_peak']:.2f}\n"
        f"Drawdown: {data['drawdown']:.2f}%"
    )


def panic_signal_triggered(data):
    fg_value = None if data["cnn_fg"] is None else data["cnn_fg"]["value"]

    return (
        fg_value is not None
        and data["vix_price"] > 30
        and fg_value < 25
        and data["drawdown"] < -10
    )


def run_panic_check(send_normal_status=False):
    global last_panic_alert_time

    data = get_market_data()

    print("VIX:", data["vix_price"])
    print("CNN Fear & Greed:", data["cnn_fg"])
    print("Drawdown:", data["drawdown"])

    if send_normal_status:
        msg = f"{format_market_status()}\n\n{format_watchlist_status()}"
        send_telegram_message(msg)

    if panic_signal_triggered(data):
        now = time.time()

        if now - last_panic_alert_time > PANIC_ALERT_COOLDOWN_SECONDS:
            fg_value = round(data["cnn_fg"]["value"]) if data["cnn_fg"] else "N/A"
            fg_desc = data["cnn_fg"]["description"] if data["cnn_fg"] else "unavailable"

            panic_msg = (
                "🚨 STOCK PANIC ALERT 🚨\n"
                f"VIX: {data['vix_price']:.2f}\n"
                f"CNN Fear & Greed: {fg_value} ({fg_desc})\n"
                f"S&P 500 Drawdown: {data['drawdown']:.2f}%\n\n"
                f"{format_watchlist_status()}"
            )

            if send_telegram_message(panic_msg):
                last_panic_alert_time = now
        else:
            print("Panic signal triggered, but cooldown active.")

    return data


def handle_command(text):
    cmd = text.strip().lower()

    if cmd == "/start":
        send_telegram_message(
            "✅ Brian Stock Bot is live.\n\n"
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
            send_telegram_message(f"{format_market_status()}\n\n{format_watchlist_status()}")
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
            data = run_panic_check(send_normal_status=False)
            fg_value = round(data["cnn_fg"]["value"]) if data["cnn_fg"] else "N/A"
            fg_desc = data["cnn_fg"]["description"] if data["cnn_fg"] else "unavailable"
            triggered = panic_signal_triggered(data)

            send_telegram_message(
                "🧪 Panic Check Complete\n"
                f"VIX: {data['vix_price']:.2f}\n"
                f"CNN Fear & Greed: {fg_value} ({fg_desc})\n"
                f"Drawdown: {data['drawdown']:.2f}%\n"
                f"Signal: {'ON' if triggered else 'OFF'}"
            )
        except Exception as e:
            print("Panic command error:", e)
            send_telegram_message(f"⚠️ /panic failed: {e}")

    else:
        send_telegram_message("Unknown command.\nUse /help to see available commands.")


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
        "Using CNN Fear & Greed + full watchlist"
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
