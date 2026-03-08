import os
import json
import time
import requests
import yfinance as yf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =============================
# CONFIG
# =============================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ET = ZoneInfo("America/New_York")

# how often to run automatic market checks
MARKET_CHECK_SECONDS = 60

# how often to poll Telegram for new commands
TELEGRAM_POLL_SECONDS = 3
TELEGRAM_GETUPDATES_TIMEOUT = 2

PANIC_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
BOND_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
CRYPTO_UPDATE_HOURS = 2

STATE_FILE = "bot_state.json"

VIX_TICKER = "^VIX"
SP_TICKER = "^GSPC"
BTC_TICKER = "BTC-USD"
ETH_TICKER = "ETH-USD"
XRP_TICKER = "XRP-USD"
OIL_TICKER = "CL=F"
BOND_TICKER = "^TNX"

# =============================
# PORTFOLIO
# Edit this with your real holdings
# =============================

PORTFOLIO = {
    # "NVDA": 110,
    # "META": 55,
    # "GOOGL": 55,
    # "VTI": 22,
    # "VOO": 12,
    # "SOFI": 800,
}

# =============================
# STATE
# =============================

state = {
    "last_panic_alert_time": 0,
    "last_regime": None,
    "last_crypto_update": None,   # ISO string
    "last_oil_alert_day": None,   # YYYY-MM-DD
    "last_bond_alert_time": 0,
    "last_buy_zone_active": False,
    "last_update_id": None,
}

# =============================
# STATE HELPERS
# =============================

def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    state.update(saved)
        except Exception as e:
            print("State load error:", e)

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print("State save error:", e)

def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None

# =============================
# TELEGRAM
# =============================

def send_telegram_message(text, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        print("Missing TELEGRAM_BOT_TOKEN")
        return False

    target_chat_id = chat_id if chat_id is not None else TELEGRAM_CHAT_ID
    if not target_chat_id:
        print("Missing TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        r = requests.post(
            url,
            json={
                "chat_id": str(target_chat_id),
                "text": text,
            },
            timeout=20,
        )
        r.raise_for_status()
        print("Telegram:", text)
        return True
    except Exception as e:
        print("Telegram error:", e)
        return False

def get_telegram_updates():
    if not TELEGRAM_BOT_TOKEN:
        return []

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "timeout": TELEGRAM_GETUPDATES_TIMEOUT,
    }

    if state.get("last_update_id") is not None:
        params["offset"] = int(state["last_update_id"]) + 1

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()

        if not data.get("ok"):
            return []

        updates = data.get("result", [])

        if updates:
            state["last_update_id"] = updates[-1]["update_id"]
            save_state()

        return updates

    except Exception as e:
        print("Telegram update error:", e)
        return []

def bootstrap_telegram_offset():
    """
    Ignore old queued commands when the bot starts/restarts.
    """
    if not TELEGRAM_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    try:
        r = requests.get(url, params={"timeout": 0}, timeout=20)
        r.raise_for_status()
        data = r.json()
        updates = data.get("result", [])
        if updates:
            state["last_update_id"] = updates[-1]["update_id"]
            save_state()
    except Exception as e:
        print("Bootstrap update offset error:", e)

# =============================
# SAFE REQUEST
# =============================

def safe_request(url):
    for _ in range(3):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return r
        except Exception as e:
            print("Retry request:", e)
            time.sleep(2)
    raise Exception("Request failed")

# =============================
# FEAR & GREED
# =============================

def get_fear_greed():
    try:
        r = safe_request("https://api.alternative.me/fng/")
        data = r.json()
        item = data["data"][0]
        return {
            "value": float(item["value"]),
            "description": item["value_classification"].lower(),
            "source": "alternative.me",
        }
    except Exception as e:
        print("Fear & Greed API failed:", e)
        return None

# =============================
# MARKET DATA
# =============================

def get_last_price_stats(ticker):
    asset = yf.Ticker(ticker)
    hist = asset.history(period="10d")

    closes = hist["Close"].dropna()
    if len(closes) < 2:
        raise ValueError(f"Not enough price history for {ticker}")

    latest = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])
    pct_change = ((latest - prev) / prev) * 100
    return latest, prev, pct_change

def get_market_data():
    vix_price, _, vix_change = get_last_price_stats(VIX_TICKER)

    sp = yf.Ticker(SP_TICKER)
    hist = sp.history(period="1y")
    closes = hist["Close"].dropna()

    if len(closes) == 0:
        raise ValueError("No S&P data available")

    sp_current = float(closes.iloc[-1])
    sp_peak = float(closes.max())
    drawdown = (sp_current - sp_peak) / sp_peak * 100

    fear_greed = get_fear_greed()

    return {
        "vix_price": vix_price,
        "vix_change": vix_change,
        "sp_current": sp_current,
        "sp_peak": sp_peak,
        "drawdown": drawdown,
        "fear_greed": fear_greed,
    }

# =============================
# FORMATTERS
# =============================

def format_market_snapshot(data):
    lines = [
        "📊 Market Snapshot",
        "",
        f"VIX: {data['vix_price']:.2f} ({data['vix_change']:+.2f}%)",
        f"S&P 500: {data['sp_current']:.2f}",
        f"Drawdown from 1Y peak: {data['drawdown']:.2f}%",
        f"Regime: {detect_market_regime(data)}",
    ]

    if data["fear_greed"]:
        fg = round(data["fear_greed"]["value"])
        desc = data["fear_greed"]["description"]
        lines.append(f"Fear & Greed: {fg} ({desc})")
    else:
        lines.append("Fear & Greed: unavailable")

    return "\n".join(lines)

def format_quote(ticker):
    price, prev, pct = get_last_price_stats(ticker)
    return (
        f"📈 {ticker.upper()}\n"
        f"Price: {price:,.2f}\n"
        f"Prev Close: {prev:,.2f}\n"
        f"Daily Move: {pct:+.2f}%"
    )

def format_crypto_prices():
    cryptos = {
        "Bitcoin": BTC_TICKER,
        "Ethereum": ETH_TICKER,
        "Ripple": XRP_TICKER,
    }

    lines = ["💰 Crypto Prices"]
    for name, ticker in cryptos.items():
        try:
            price, _, pct = get_last_price_stats(ticker)
            lines.append(f"{name}: {price:,.2f} ({pct:+.2f}%)")
        except Exception:
            lines.append(f"{name}: error")
    return "\n".join(lines)

def format_portfolio():
    if not PORTFOLIO:
        return (
            "📁 Portfolio is empty.\n\n"
            "Edit PORTFOLIO at the top of the script, for example:\n"
            'PORTFOLIO = {"NVDA": 110, "META": 55, "GOOGL": 55}'
        )

    lines = ["📁 Portfolio"]
    total_value = 0.0
    total_daily_pnl = 0.0

    for ticker, shares in PORTFOLIO.items():
        try:
            price, prev, pct = get_last_price_stats(ticker)
            value = price * shares
            daily_pnl = (price - prev) * shares

            total_value += value
            total_daily_pnl += daily_pnl

            lines.append(
                f"{ticker}: {shares} sh | {price:,.2f} ({pct:+.2f}%) | ${value:,.2f}"
            )
        except Exception:
            lines.append(f"{ticker}: error")

    lines.append("")
    lines.append(f"Total Value: ${total_value:,.2f}")
    lines.append(f"Daily P/L: ${total_daily_pnl:+,.2f}")

    return "\n".join(lines)

# =============================
# MARKET REGIME
# =============================

def detect_market_regime(data):
    vix = data["vix_price"]
    drawdown = data["drawdown"]
    fg = data["fear_greed"]["value"] if data["fear_greed"] else None

    if vix >= 35 and drawdown <= -12:
        return "CRISIS"

    if vix >= 27 or (fg is not None and fg < 30):
        return "RISK OFF"

    if vix < 18 and (fg is not None and fg > 60):
        return "RISK ON"

    return "NEUTRAL"

def maybe_send_regime_alert(data):
    regime = detect_market_regime(data)

    if regime != state.get("last_regime"):
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

        if send_telegram_message(msg):
            state["last_regime"] = regime
            save_state()

# =============================
# PANIC SIGNAL
# =============================

def panic_signal_triggered(data):
    fg = data["fear_greed"]["value"] if data["fear_greed"] else None

    return (
        data["vix_price"] >= 30
        and data["drawdown"] <= -8
        and fg is not None
        and fg < 30
    )

def run_panic_check():
    data = get_market_data()

    if panic_signal_triggered(data):
        now_ts = time.time()

        if now_ts - state.get("last_panic_alert_time", 0) > PANIC_ALERT_COOLDOWN_SECONDS:
            fg = round(data["fear_greed"]["value"]) if data["fear_greed"] else "N/A"

            msg = (
                "🚨 STOCK PANIC ALERT 🚨\n\n"
                f"VIX: {data['vix_price']:.2f}\n"
                f"Fear & Greed: {fg}\n"
                f"Drawdown: {data['drawdown']:.2f}%"
            )

            if send_telegram_message(msg):
                state["last_panic_alert_time"] = now_ts
                save_state()

    return data

# =============================
# BUY ZONE
# =============================

def check_buy_zone(data):
    active = data["drawdown"] < -6 and data["vix_price"] > 25
    was_active = bool(state.get("last_buy_zone_active", False))

    if active and not was_active:
        msg = (
            "🟢 BUY ZONE DETECTED\n\n"
            f"Drawdown: {data['drawdown']:.2f}%\n"
            f"VIX: {data['vix_price']:.2f}"
        )
        if send_telegram_message(msg):
            state["last_buy_zone_active"] = True
            save_state()

    elif not active and was_active:
        state["last_buy_zone_active"] = False
        save_state()

# =============================
# OIL ALERT
# =============================

def check_oil_spike():
    try:
        price, _, pct = get_last_price_stats(OIL_TICKER)
        today = datetime.now(ET).date().isoformat()

        if pct > 5 and state.get("last_oil_alert_day") != today:
            msg = (
                "⚠️ OIL SPIKE\n"
                f"Crude: {price:.2f}\n"
                f"Move: {pct:+.2f}%"
            )

            if send_telegram_message(msg):
                state["last_oil_alert_day"] = today
                save_state()

    except Exception as e:
        print("Oil check error:", e)

# =============================
# BOND ALERT
# =============================

def check_bond_spike():
    try:
        price, _, pct = get_last_price_stats(BOND_TICKER)
        now_ts = time.time()

        if abs(pct) > 3 and now_ts - state.get("last_bond_alert_time", 0) > BOND_ALERT_COOLDOWN_SECONDS:
            msg = (
                "🏦 BOND YIELD MOVE\n"
                f"10Y: {price:.2f}\n"
                f"Move: {pct:+.2f}%"
            )

            if send_telegram_message(msg):
                state["last_bond_alert_time"] = now_ts
                save_state()

    except Exception as e:
        print("Bond check error:", e)

# =============================
# CRYPTO
# =============================

def maybe_send_crypto_update(now_dt):
    last_sent = parse_iso_datetime(state.get("last_crypto_update"))

    if last_sent is None or (now_dt - last_sent) > timedelta(hours=CRYPTO_UPDATE_HOURS):
        msg = format_crypto_prices()
        if send_telegram_message(msg):
            state["last_crypto_update"] = now_dt.isoformat()
            save_state()

# =============================
# COMMAND HANDLER
# =============================

def handle_command(text):
    text = text.strip()

    if not text:
        return None

    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    # support /price@YourBotName
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]

    # also allow plain words like "price"
    if not cmd.startswith("/"):
        cmd = "/" + cmd

    try:
        if cmd in ("/start", "/help"):
            return (
                "📘 Commands\n\n"
                "/price - market snapshot\n"
                "/portfolio - your portfolio prices\n"
                "/quote NVDA - quote for any ticker\n"
                "/crypto - BTC / ETH / XRP\n"
                "/vix - VIX check\n"
                "/oil - oil price\n"
                "/bond - 10Y yield\n"
                "/regime - market regime\n"
                "/panic - panic signal status\n"
                "/help - command list"
            )

        if cmd == "/price":
            data = get_market_data()
            return format_market_snapshot(data)

        if cmd == "/portfolio":
            return format_portfolio()

        if cmd == "/quote":
            if not args:
                return "Usage: /quote NVDA"
            ticker = args[0].upper()
            return format_quote(ticker)

        if cmd == "/crypto":
            return format_crypto_prices()

        if cmd == "/vix":
            return format_quote(VIX_TICKER)

        if cmd == "/oil":
            return format_quote(OIL_TICKER)

        if cmd == "/bond":
            return format_quote(BOND_TICKER)

        if cmd == "/regime":
            data = get_market_data()
            return (
                f"🌎 Regime: {detect_market_regime(data)}\n\n"
                + format_market_snapshot(data)
            )

        if cmd == "/panic":
            data = get_market_data()
            triggered = panic_signal_triggered(data)
            return (
                "🚨 Panic signal: ON\n\n" + format_market_snapshot(data)
                if triggered
                else "✅ Panic signal: OFF\n\n" + format_market_snapshot(data)
            )

        return "Unknown command. Type /help"

    except Exception as e:
        print("Command handler error:", e)
        return f"Command error: {e}"

def check_telegram_commands():
    updates = get_telegram_updates()

    for update in updates:
        if "message" not in update:
            continue

        msg = update["message"]
        chat_id = str(msg.get("chat", {}).get("id", ""))

        # only allow your approved chat ID
        if str(chat_id) != str(TELEGRAM_CHAT_ID):
            continue

        text = msg.get("text")
        if not text:
            continue

        response = handle_command(text)
        if response:
            send_telegram_message(response, chat_id=chat_id)

# =============================
# MAIN
# =============================

def validate_config():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN environment variable")
    if not TELEGRAM_CHAT_ID:
        raise ValueError("Missing TELEGRAM_CHAT_ID environment variable")

def main():
    validate_config()
    load_state()
    bootstrap_telegram_offset()

    send_telegram_message("✅ Brian Market Bot Started")

    next_market_check = 0

    while True:
        try:
            # fast command polling
            check_telegram_commands()

            # slower automatic checks
            now_ts = time.time()
            if now_ts >= next_market_check:
                now_dt = datetime.now(ET)

                data = run_panic_check()
                maybe_send_regime_alert(data)
                check_buy_zone(data)
                check_oil_spike()
                check_bond_spike()
                maybe_send_crypto_update(now_dt)

                next_market_check = now_ts + MARKET_CHECK_SECONDS

            time.sleep(TELEGRAM_POLL_SECONDS)

        except Exception as e:
            print("Main loop error:", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
