import os
import re
import json
import html
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
ASIA_TZ = ZoneInfo("Asia/Tokyo")  # Japan + Korea bot timing

MARKET_CHECK_SECONDS = 60
TELEGRAM_POLL_SECONDS = 3
TELEGRAM_GETUPDATES_TIMEOUT = 2

PANIC_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
BOND_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
CRYPTO_UPDATE_HOURS = 2

STATE_FILE = "bot_state.json"

VIX_TICKER = "^VIX"
SP_TICKER = "^GSPC"
OIL_TICKER = "CL=F"
BOND_TICKER = "^TNX"

BTC_TICKER = "BTC-USD"
ETH_TICKER = "ETH-USD"
XRP_TICKER = "XRP-USD"

# =============================
# WATCHLIST / "PORTFOLIO"
# Note: this is a ticker watchlist, not share-count holdings
# =============================

PORTFOLIO_WATCHLIST = {
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
    "Bitcoin": "BTC-USD",
    "Dow Futures": "YM=F",
    "Nasdaq Futures": "NQ=F",
    "S&P Futures": "ES=F",
}

CRYPTO_WATCHLIST = {
    "Bitcoin": BTC_TICKER,
    "Ethereum": ETH_TICKER,
    "Ripple": XRP_TICKER,
}

JAPAN_MARKETS = {
    "Nikkei 225": "^N225",
    "TOPIX": "^TOPX",
}

KOREA_MARKETS = {
    "KOSPI": "^KS11",
    "KOSDAQ": "^KQ11",
}

FUTURES_WATCHLIST = {
    "Dow Futures": "YM=F",
    "Nasdaq Futures": "NQ=F",
    "S&P Futures": "ES=F",
}

# =============================
# STATE
# =============================

state = {
    "last_panic_alert_time": 0,
    "last_regime": None,
    "last_crypto_update": None,
    "last_oil_alert_day": None,
    "last_bond_alert_time": 0,
    "last_buy_zone_active": False,
    "last_update_id": None,
    "last_asia_open_alert_date": None,
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

def safe_request(url, params=None, headers=None):
    for _ in range(3):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            return r
        except Exception as e:
            print("Retry request:", e)
            time.sleep(2)
    raise Exception(f"Request failed: {url}")

# =============================
# SENTIMENT
# =============================

def get_crypto_fear_greed():
    """
    Reliable crypto fear & greed from alternative.me
    """
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
        print("Crypto Fear & Greed API failed:", e)
        return None

def get_stock_fear_greed():
    """
    Stock-market fear & greed from a public webpage.
    Falls back later to a proxy if parsing fails.
    """
    try:
        r = safe_request(
            "https://feargreedmeter.com/",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        page = r.text
        text = re.sub(r"<script.*?</script>", " ", page, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()

        # Example target:
        # "Now Fear 27 Yesterday Fear 33"
        m = re.search(
            r"Now\s+([A-Za-z ]+?)\s+(\d{1,3})\s+Yesterday",
            text,
            flags=re.I
        )
        if m:
            return {
                "value": float(m.group(2)),
                "description": m.group(1).strip().lower(),
                "source": "feargreedmeter.com",
            }

        # backup pattern
        m2 = re.search(
            r"Fear and Greed Index.*?Now\s+([A-Za-z ]+?)\s+(\d{1,3})",
            text,
            flags=re.I
        )
        if m2:
            return {
                "value": float(m2.group(2)),
                "description": m2.group(1).strip().lower(),
                "source": "feargreedmeter.com",
            }

        raise ValueError("Could not parse stock fear & greed")
    except Exception as e:
        print("Stock Fear & Greed scrape failed:", e)
        return None

def build_stock_fear_greed_proxy(vix, drawdown, sp_above_50dma, sp_above_200dma):
    """
    Fallback score if webpage scraping breaks.
    0 = extreme fear, 100 = extreme greed
    """
    score = 50

    # VIX contribution
    if vix >= 35:
        score -= 30
    elif vix >= 30:
        score -= 22
    elif vix >= 25:
        score -= 15
    elif vix >= 20:
        score -= 8
    elif vix <= 14:
        score += 18
    elif vix <= 17:
        score += 10

    # Drawdown contribution
    if drawdown <= -15:
        score -= 25
    elif drawdown <= -10:
        score -= 18
    elif drawdown <= -6:
        score -= 10
    elif drawdown <= -3:
        score -= 5
    elif drawdown >= 3:
        score += 15
    elif drawdown >= 1:
        score += 8

    # Trend contribution
    if sp_above_50dma:
        score += 8
    else:
        score -= 8

    if sp_above_200dma:
        score += 12
    else:
        score -= 12

    score = max(0, min(100, int(round(score))))

    if score <= 24:
        desc = "extreme fear"
    elif score <= 44:
        desc = "fear"
    elif score <= 55:
        desc = "neutral"
    elif score <= 74:
        desc = "greed"
    else:
        desc = "extreme greed"

    return {
        "value": float(score),
        "description": desc,
        "source": "proxy",
    }

# =============================
# MARKET DATA
# =============================

def get_last_price_and_change(ticker):
    """
    Tries fast_info first, then daily history.
    Returns (latest_price, daily_pct_change)
    """
    asset = yf.Ticker(ticker)

    # Try fast_info
    try:
        fi = asset.fast_info
        latest = fi.get("lastPrice")
        prev_close = fi.get("previousClose")
        if latest is not None and prev_close not in (None, 0):
            latest = float(latest)
            prev_close = float(prev_close)
            pct_change = ((latest - prev_close) / prev_close) * 100
            return latest, pct_change
    except Exception as e:
        print(f"fast_info failed for {ticker}:", e)

    # Fallback to daily history
    hist = asset.history(period="10d", interval="1d", auto_adjust=False)
    closes = hist["Close"].dropna()

    if len(closes) < 2:
        raise ValueError(f"Not enough price history for {ticker}")

    latest = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])
    pct_change = ((latest - prev) / prev) * 100
    return latest, pct_change

def get_market_data():
    vix_price, vix_change = get_last_price_and_change(VIX_TICKER)

    sp = yf.Ticker(SP_TICKER)
    hist = sp.history(period="1y", interval="1d", auto_adjust=False)
    closes = hist["Close"].dropna()

    if len(closes) < 200:
        raise ValueError("Not enough S&P history")

    sp_current = float(closes.iloc[-1])
    sp_peak = float(closes.max())
    drawdown = (sp_current - sp_peak) / sp_peak * 100

    sma50 = float(closes.tail(50).mean())
    sma200 = float(closes.tail(200).mean())

    stock_fg = get_stock_fear_greed()
    if stock_fg is None:
        stock_fg = build_stock_fear_greed_proxy(
            vix=vix_price,
            drawdown=drawdown,
            sp_above_50dma=(sp_current >= sma50),
            sp_above_200dma=(sp_current >= sma200),
        )

    crypto_fg = get_crypto_fear_greed()

    return {
        "vix_price": vix_price,
        "vix_change": vix_change,
        "sp_current": sp_current,
        "sp_peak": sp_peak,
        "drawdown": drawdown,
        "sma50": sma50,
        "sma200": sma200,
        "stock_fear_greed": stock_fg,
        "crypto_fear_greed": crypto_fg,
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

    stock_fg = data.get("stock_fear_greed")
    if stock_fg:
        lines.append(
            f"Stock Fear & Greed: {int(round(stock_fg['value']))} "
            f"({stock_fg['description']}, {stock_fg['source']})"
        )

    crypto_fg = data.get("crypto_fear_greed")
    if crypto_fg:
        lines.append(
            f"Crypto Fear & Greed: {int(round(crypto_fg['value']))} "
            f"({crypto_fg['description']}, {crypto_fg['source']})"
        )

    return "\n".join(lines)

def format_quote(name, ticker):
    price, pct = get_last_price_and_change(ticker)
    return f"{name}: {price:,.2f} ({pct:+.2f}%)"

def format_watchlist(title, watchlist):
    lines = [title]
    for name, ticker in watchlist.items():
        try:
            lines.append(format_quote(name, ticker))
        except Exception:
            lines.append(f"{name}: error")
    return "\n".join(lines)

def format_portfolio_watchlist():
    return format_watchlist("📁 Portfolio Watchlist", PORTFOLIO_WATCHLIST)

def format_crypto_prices():
    return format_watchlist("💰 Crypto Prices", CRYPTO_WATCHLIST)

def format_futures():
    return format_watchlist("📉 Futures", FUTURES_WATCHLIST)

def format_japan_markets():
    return format_watchlist("🇯🇵 Japan Markets", JAPAN_MARKETS)

def format_korea_markets():
    return format_watchlist("🇰🇷 Korea Markets", KOREA_MARKETS)

def format_asia_markets():
    lines = [
        "🌏 Asia Markets",
        "",
        "Japan:",
    ]
    for name, ticker in JAPAN_MARKETS.items():
        try:
            lines.append(f"  {format_quote(name, ticker)}")
        except Exception:
            lines.append(f"  {name}: error")

    lines.append("")
    lines.append("Korea:")
    for name, ticker in KOREA_MARKETS.items():
        try:
            lines.append(f"  {format_quote(name, ticker)}")
        except Exception:
            lines.append(f"  {name}: error")

    return "\n".join(lines)

# =============================
# MARKET REGIME
# =============================

def detect_market_regime(data):
    vix = data["vix_price"]
    drawdown = data["drawdown"]
    fg = None

    stock_fg = data.get("stock_fear_greed")
    if stock_fg:
        fg = stock_fg["value"]

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

        stock_fg = data.get("stock_fear_greed")
        if stock_fg:
            msg += (
                f"\nStock Fear & Greed: {int(round(stock_fg['value']))} "
                f"({stock_fg['description']}, {stock_fg['source']})"
            )

        if send_telegram_message(msg):
            state["last_regime"] = regime
            save_state()

# =============================
# PANIC SIGNAL
# =============================

def panic_signal_triggered(data):
    fg = None
    stock_fg = data.get("stock_fear_greed")
    if stock_fg:
        fg = stock_fg["value"]

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
            stock_fg = data.get("stock_fear_greed")
            fg_text = (
                f"{int(round(stock_fg['value']))} ({stock_fg['description']})"
                if stock_fg else "N/A"
            )

            msg = (
                "🚨 STOCK PANIC ALERT 🚨\n\n"
                f"VIX: {data['vix_price']:.2f}\n"
                f"Stock Fear & Greed: {fg_text}\n"
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
# OIL / BONDS
# =============================

def check_oil_spike():
    try:
        price, pct = get_last_price_and_change(OIL_TICKER)
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

def check_bond_spike():
    try:
        price, pct = get_last_price_and_change(BOND_TICKER)
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
# CRYPTO UPDATE
# =============================

def maybe_send_crypto_update(now_dt):
    last_sent = parse_iso_datetime(state.get("last_crypto_update"))

    if last_sent is None or (now_dt - last_sent) > timedelta(hours=CRYPTO_UPDATE_HOURS):
        msg = format_crypto_prices()
        if send_telegram_message(msg):
            state["last_crypto_update"] = now_dt.isoformat()
            save_state()

# =============================
# ASIA OPEN ALERT
# =============================

def maybe_send_asia_open_snapshot():
    now_asia = datetime.now(ASIA_TZ)
    today = now_asia.date().isoformat()

    # Weekdays only
    if now_asia.weekday() >= 5:
        return

    # Send once shortly after 9am local time
    # Practical choice for Tokyo/Seoul open snapshot
    if now_asia.hour == 9 and now_asia.minute <= 10:
        if state.get("last_asia_open_alert_date") == today:
            return

        msg = format_asia_markets()
        if send_telegram_message("🔔 Asia Open\n\n" + msg):
            state["last_asia_open_alert_date"] = today
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

    # support plain words too
    if not cmd.startswith("/"):
        cmd = "/" + cmd

    try:
        if cmd in ("/start", "/help"):
            return (
                "📘 Commands\n\n"
                "/price - market snapshot\n"
                "/portfolio - your ticker watchlist\n"
                "/quote NVDA - quote any ticker\n"
                "/crypto - BTC / ETH / XRP\n"
                "/futures - Dow / Nasdaq / S&P futures\n"
                "/vix - VIX check\n"
                "/oil - oil price\n"
                "/bond - 10Y yield\n"
                "/regime - market regime\n"
                "/panic - panic signal status\n"
                "/sentiment - stock + crypto fear/greed\n"
                "/japan - Nikkei + TOPIX\n"
                "/korea - KOSPI + KOSDAQ\n"
                "/asia - Japan + Korea snapshot\n"
                "/help - command list"
            )

        if cmd == "/price":
            data = get_market_data()
            return format_market_snapshot(data)

        if cmd == "/portfolio":
            return format_portfolio_watchlist()

        if cmd == "/quote":
            if not args:
                return "Usage: /quote NVDA"
            ticker = args[0].upper()
            return format_quote(ticker, ticker)

        if cmd == "/crypto":
            return format_crypto_prices()

        if cmd == "/futures":
            return format_futures()

        if cmd == "/vix":
            return format_quote("VIX", VIX_TICKER)

        if cmd == "/oil":
            return format_quote("Crude Oil", OIL_TICKER)

        if cmd == "/bond":
            return format_quote("US 10Y Yield", BOND_TICKER)

        if cmd == "/regime":
            data = get_market_data()
            return f"🌎 Regime: {detect_market_regime(data)}\n\n{format_market_snapshot(data)}"

        if cmd == "/panic":
            data = get_market_data()
            if panic_signal_triggered(data):
                return "🚨 Panic signal: ON\n\n" + format_market_snapshot(data)
            return "✅ Panic signal: OFF\n\n" + format_market_snapshot(data)

        if cmd == "/sentiment":
            data = get_market_data()
            stock_fg = data.get("stock_fear_greed")
            crypto_fg = data.get("crypto_fear_greed")

            lines = ["😬 Sentiment"]
            if stock_fg:
                lines.append(
                    f"Stock Fear & Greed: {int(round(stock_fg['value']))} "
                    f"({stock_fg['description']}, {stock_fg['source']})"
                )
            if crypto_fg:
                lines.append(
                    f"Crypto Fear & Greed: {int(round(crypto_fg['value']))} "
                    f"({crypto_fg['description']}, {crypto_fg['source']})"
                )
            return "\n".join(lines)

        if cmd == "/japan":
            return format_japan_markets()

        if cmd == "/korea":
            return format_korea_markets()

        if cmd == "/asia":
            return format_asia_markets()

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

            # scheduled checks
            now_ts = time.time()
            if now_ts >= next_market_check:
                now_et = datetime.now(ET)

                data = run_panic_check()
                maybe_send_regime_alert(data)
                check_buy_zone(data)
                check_oil_spike()
                check_bond_spike()
                maybe_send_crypto_update(now_et)
                maybe_send_asia_open_snapshot()

                next_market_check = now_ts + MARKET_CHECK_SECONDS

            time.sleep(TELEGRAM_POLL_SECONDS)

        except Exception as e:
            print("Main loop error:", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
