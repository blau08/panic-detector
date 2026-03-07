import os
import time
import requests
import yfinance as yf
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ET = ZoneInfo("America/New_York")

MAIN_LOOP_SECONDS = 60
PANIC_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
BITCOIN_MOVE_ALERT_THRESHOLD = 2.0

STOCK_TICKERS = [
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

FUTURES_TICKERS = [
    "YM=F",   # Dow futures
    "NQ=F",   # Nasdaq futures
    "ES=F",   # S&P 500 futures
]

BITCOIN_TICKER = "BTC-USD"
VIX_TICKER = "^VIX"

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
    "YM=F": "Dow Futures",
    "NQ=F": "Nasdaq Futures",
    "ES=F": "S&P Futures",
    "BTC-USD": "Bitcoin",
    "^VIX": "VIX",
}

last_panic_alert_time = 0
last_update_id = None

last_stock_hour_sent = None
last_futures_slot_sent = None
last_btc_send_time = None
last_btc_price = None
last_vix_price = None
last_vix_bucket = None


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
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

    try:
        r = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()
        data = r.json()

        score = None
        rating = "unavailable"

        if isinstance(data, dict):
            block = data.get("fear_and_greed") or data.get("fear_and_greed_historical")
            if isinstance(block, dict):
                score = block.get("score")
                rating = block.get("rating", rating)

            if score is None:
                score = data.get("score", data.get("value"))
            if "rating" in data:
                rating = data.get("rating", rating)

        if score is None:
            raise ValueError("CNN Fear & Greed score not found")

        return {
            "value": float(score),
            "description": str(rating),
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


def get_intraday_price(ticker):
    stock = yf.Ticker(ticker)
    hist = stock.history(period="2d", interval="30m")

    if hist.empty or hist["Close"].dropna().empty:
        # fallback to daily
        latest, _ = get_last_price_and_change(ticker)
        return latest

    return float(hist["Close"].dropna().iloc[-1])


def format_ticker_block(title, tickers):
    lines = [title]

    for ticker in tickers:
        label = LABEL_MAP.get(ticker, ticker)
        try:
            price, pct = get_last_price_and_change(ticker)
            lines.append(f"{label}: {price:.2f} ({pct:+.2f}%)")
        except Exception as e:
            print(f"Ticker block error for {ticker}: {e}")
            lines.append(f"{label}: error")

    return "\n".join(lines)


def get_market_data():
    vix_price, vix_change = get_last_price_and_change(VIX_TICKER)

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
        send_telegram_message(format_market_status())

    if panic_signal_triggered(data):
        now_ts = time.time()

        if now_ts - last_panic_alert_time > PANIC_ALERT_COOLDOWN_SECONDS:
            fg_value = round(data["cnn_fg"]["value"]) if data["cnn_fg"] else "N/A"
            fg_desc = data["cnn_fg"]["description"] if data["cnn_fg"] else "unavailable"

            panic_msg = (
                "🚨 STOCK PANIC ALERT 🚨\n"
                f"VIX: {data['vix_price']:.2f}\n"
                f"CNN Fear & Greed: {fg_value} ({fg_desc})\n"
                f"S&P 500 Drawdown: {data['drawdown']:.2f}%"
            )

            if send_telegram_message(panic_msg):
                last_panic_alert_time = now_ts
        else:
            print("Panic signal triggered, but cooldown active.")

    return data


def is_weekday_et(now_et):
    return now_et.weekday() < 5


def is_stock_market_open(now_et):
    if not is_weekday_et(now_et):
        return False

    current_time = now_et.time()
    return dt_time(9, 30) <= current_time < dt_time(16, 0)


def get_stock_hour_key(now_et):
    return now_et.strftime("%Y-%m-%d %H")


def get_futures_slot(now_et):
    if not is_weekday_et(now_et):
        return None

    current_time = now_et.time()

    # useful checkpoints
    if dt_time(8, 30) <= current_time < dt_time(8, 40):
        return now_et.strftime("%Y-%m-%d preopen-0830")
    if dt_time(16, 15) <= current_time < dt_time(16, 25):
        return now_et.strftime("%Y-%m-%d close-1615")
    if dt_time(18, 0) <= current_time < dt_time(18, 10):
        return now_et.strftime("%Y-%m-%d night-1800")

    return None


def get_vix_bucket(vix_price):
    if vix_price >= 30:
        return "30+"
    if vix_price >= 25:
        return "25-29.99"
    if vix_price >= 20:
        return "20-24.99"
    return "<20"


def maybe_send_stock_update(now_et):
    global last_stock_hour_sent

    if not is_stock_market_open(now_et):
        return

    hour_key = get_stock_hour_key(now_et)
    if hour_key == last_stock_hour_sent:
        return

    msg = format_ticker_block("📈 Hourly Stock Update", STOCK_TICKERS)
    if send_telegram_message(msg):
        last_stock_hour_sent = hour_key


def maybe_send_futures_update(now_et):
    global last_futures_slot_sent

    slot = get_futures_slot(now_et)
    if slot is None or slot == last_futures_slot_sent:
        return

    msg = format_ticker_block("🌙 Futures Check", FUTURES_TICKERS)
    if send_telegram_message(msg):
        last_futures_slot_sent = slot


def maybe_send_bitcoin_update(now_et):
    global last_btc_send_time, last_btc_price

    try:
        current_price = get_intraday_price(BITCOIN_TICKER)
    except Exception as e:
        print("Bitcoin update error:", e)
        return

    send_due_to_time = (
        last_btc_send_time is None or
        (now_et - last_btc_send_time) >= timedelta(hours=2)
    )

    send_due_to_move = False
    move_pct = 0.0

    if last_btc_price is not None and last_btc_price != 0:
        move_pct = ((current_price - last_btc_price) / last_btc_price) * 100
        if abs(move_pct) >= BITCOIN_MOVE_ALERT_THRESHOLD:
            send_due_to_move = True

    if send_due_to_time or send_due_to_move:
        if send_due_to_move and last_btc_price is not None:
            text = (
                "₿ Bitcoin Alert\n"
                f"Bitcoin: {current_price:.2f}\n"
                f"Move since last BTC check: {move_pct:+.2f}%"
            )
        else:
            try:
                daily_price, daily_pct = get_last_price_and_change(BITCOIN_TICKER)
                text = (
                    "₿ Bitcoin 2-Hour Check\n"
                    f"Bitcoin: {daily_price:.2f} ({daily_pct:+.2f}% daily)"
                )
            except Exception:
                text = (
                    "₿ Bitcoin 2-Hour Check\n"
                    f"Bitcoin: {current_price:.2f}"
                )

        if send_telegram_message(text):
            last_btc_send_time = now_et
            last_btc_price = current_price


def maybe_send_vix_alert():
    global last_vix_price, last_vix_bucket

    try:
        vix_price, vix_change = get_last_price_and_change(VIX_TICKER)
    except Exception as e:
        print("VIX alert error:", e)
        return

    current_bucket = get_vix_bucket(vix_price)
    send_alert = False
    reasons = []

    if last_vix_bucket is None:
        last_vix_bucket = current_bucket

    if current_bucket != last_vix_bucket and current_bucket in {"20-24.99", "25-29.99", "30+"}:
        send_alert = True
        reasons.append(f"crossed into {current_bucket}")

    if abs(vix_change) >= 10:
        send_alert = True
        reasons.append(f"daily move {vix_change:+.2f}%")

    if last_vix_price is not None and last_vix_price != 0:
        intrarun_move = ((vix_price - last_vix_price) / last_vix_price) * 100
        if abs(intrarun_move) >= 8:
            send_alert = True
            reasons.append(f"move since last check {intrarun_move:+.2f}%")

    if send_alert:
        reason_text = ", ".join(reasons) if reasons else "important VIX move"
        msg = (
            "⚠️ VIX Alert\n"
            f"VIX: {vix_price:.2f}\n"
            f"Daily change: {vix_change:+.2f}%\n"
            f"Reason: {reason_text}"
        )
        send_telegram_message(msg)

    last_vix_price = vix_price
    last_vix_bucket = current_bucket


def handle_command(text):
    cmd = text.strip().lower()

    if cmd == "/start":
        send_telegram_message(
            "✅ Brian Market Bot is live.\n\n"
            "Commands:\n"
            "/status - market status\n"
            "/stocks - stock watchlist\n"
            "/futures - futures only\n"
            "/btc - bitcoin now\n"
            "/watchlist - all watchlists\n"
            "/panic - run panic check now\n"
            "/help - show commands"
        )

    elif cmd == "/help":
        send_telegram_message(
            "Commands:\n"
            "/status - market status\n"
            "/stocks - stock watchlist\n"
            "/futures - futures only\n"
            "/btc - bitcoin now\n"
            "/watchlist - all watchlists\n"
            "/panic - run panic check now\n"
            "/help - show commands"
        )

    elif cmd == "/status":
        try:
            send_telegram_message(format_market_status())
        except Exception as e:
            send_telegram_message(f"⚠️ /status failed: {e}")

    elif cmd == "/stocks":
        try:
            send_telegram_message(format_ticker_block("📈 Stocks", STOCK_TICKERS))
        except Exception as e:
            send_telegram_message(f"⚠️ /stocks failed: {e}")

    elif cmd == "/futures":
        try:
            send_telegram_message(format_ticker_block("🌙 Futures", FUTURES_TICKERS))
        except Exception as e:
            send_telegram_message(f"⚠️ /futures failed: {e}")

    elif cmd == "/btc":
        try:
            price, pct = get_last_price_and_change(BITCOIN_TICKER)
            send_telegram_message(f"₿ Bitcoin\nBitcoin: {price:.2f} ({pct:+.2f}% daily)")
        except Exception as e:
            send_telegram_message(f"⚠️ /btc failed: {e}")

    elif cmd == "/watchlist":
        try:
            parts = [
                format_ticker_block("📈 Stocks", STOCK_TICKERS),
                format_ticker_block("🌙 Futures", FUTURES_TICKERS),
            ]
            try:
                btc_price, btc_pct = get_last_price_and_change(BITCOIN_TICKER)
                parts.append(f"₿ Bitcoin\nBitcoin: {btc_price:.2f} ({btc_pct:+.2f}% daily)")
            except Exception:
                parts.append("₿ Bitcoin\nBitcoin: error")

            send_telegram_message("\n\n".join(parts))
        except Exception as e:
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
    require_env()

    send_telegram_message(
        "✅ Bot started on Railway\n"
        "Stocks: hourly during market hours\n"
        "Futures: key checkpoints\n"
        "VIX: important alerts only\n"
        "Bitcoin: every 2 hours or >2% move"
    )

    try:
        run_panic_check(send_normal_status=True)
    except Exception as e:
        print("Startup market check error:", e)
        send_telegram_message(f"⚠️ Startup market check failed: {e}")

    while True:
        try:
            now_et = datetime.now(ET)

            process_telegram_updates()
            maybe_send_stock_update(now_et)
            maybe_send_futures_update(now_et)
            maybe_send_bitcoin_update(now_et)
            maybe_send_vix_alert()
            run_panic_check(send_normal_status=False)

            time.sleep(MAIN_LOOP_SECONDS)

        except Exception as e:
            print("Main loop error:", e)
            send_telegram_message(f"⚠️ Main loop error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
