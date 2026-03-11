import os
import re
import json
import html
import time
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote
import xml.etree.ElementTree as ETXML

# =============================
# CONFIG
# =============================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ET_TZ = ZoneInfo("America/New_York")
ASIA_TZ = ZoneInfo("Asia/Tokyo")

MARKET_CHECK_SECONDS = 60
TELEGRAM_POLL_SECONDS = 3
TELEGRAM_GETUPDATES_TIMEOUT = 2

PANIC_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
BOND_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60

US_OPEN_ALERT_WINDOW_MINUTES = 15
US_FUTURES_UPDATE_HOURS = 3

ASIA_OPEN_ALERT_HOUR = 9
ASIA_OPEN_ALERT_WINDOW_MINUTES = 15

NEWSLETTER_HOUR_ASIA = 8
NEWSLETTER_MINUTE_ASIA = 30
NEWSLETTER_TOP_N = 2
NEWSLETTER_MAX_HEADLINE_LEN = 110

STATE_FILE = "bot_state.json"

VIX_TICKER = "^VIX"
SP_TICKER = "^GSPC"
SPY_TICKER = "SPY"
QQQ_TICKER = "QQQ"
OIL_TICKER = "CL=F"
BOND_TICKER = "^TNX"
HYG_TICKER = "HYG"
IEF_TICKER = "IEF"

BTC_TICKER = "BTC-USD"
ETH_TICKER = "ETH-USD"
XRP_TICKER = "XRP-USD"

AI_BASKET = {
    "NVDA": "NVDA",
    "AMD": "AMD",
    "AVGO": "AVGO",
    "TSM": "TSM",
    "MSFT": "MSFT",
    "META": "META",
    "GOOGL": "GOOGL",
}

# =============================
# YOUR WATCHLIST
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
}

CRYPTO_WATCHLIST = {
    "Bitcoin": BTC_TICKER,
    "Ethereum": ETH_TICKER,
    "Ripple": XRP_TICKER,
}

FUTURES_WATCHLIST = {
    "Dow Futures": "YM=F",
    "Nasdaq Futures": "NQ=F",
    "S&P Futures": "ES=F",
}

JAPAN_MARKETS = {
    "Nikkei 225": "^N225",
    "TOPIX": "998405.T",
}

KOREA_MARKETS = {
    "KOSPI": "^KS11",
    "KOSDAQ": "^KQ11",
}

# Keep aliases empty for TOPIX to avoid bad fallback to ^TOPX
TICKER_ALIASES = {}

# =============================
# STATE
# =============================

state = {
    "last_panic_alert_time": 0,
    "last_regime": None,
    "last_oil_alert_day": None,
    "last_bond_alert_time": 0,
    "last_buy_zone_active": False,
    "last_update_id": None,
    "last_asia_open_alert_date": None,
    "last_newsletter_date": None,
    "last_us_open_alert_date": None,
    "last_us_futures_update_bucket": None,
}

# =============================
# HELPERS
# =============================

def clamp_score(value, low=0, high=100):
    return max(low, min(high, int(round(value))))

def score_band(score):
    if score >= 65:
        return "HIGH"
    if score >= 35:
        return "MEDIUM"
    return "LOW"

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
        print("Telegram:", text[:500])
        return True
    except Exception as e:
        print("Telegram error:", e)
        return False

def get_telegram_updates():
    if not TELEGRAM_BOT_TOKEN:
        return []

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": TELEGRAM_GETUPDATES_TIMEOUT}

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
# SESSION LOGIC
# =============================

def in_us_open_alert_window(now_et):
    if now_et.weekday() >= 5:
        return False

    mins = now_et.hour * 60 + now_et.minute
    start = 9 * 60 + 30
    end = start + US_OPEN_ALERT_WINDOW_MINUTES
    return start <= mins < end

def is_us_futures_open(now_et):
    wd = now_et.weekday()
    mins = now_et.hour * 60 + now_et.minute

    if wd == 5:
        return False
    if wd == 6:
        return mins >= 18 * 60
    if wd == 4:
        return mins < 17 * 60

    if 17 * 60 <= mins < 18 * 60:
        return False

    return True

def get_futures_bucket(now_et):
    bucket_hour = (now_et.hour // US_FUTURES_UPDATE_HOURS) * US_FUTURES_UPDATE_HOURS
    return f"{now_et.strftime('%Y-%m-%d')}-{bucket_hour:02d}"

def in_asia_open_alert_window(now_asia):
    if now_asia.weekday() >= 5:
        return False

    mins = now_asia.hour * 60 + now_asia.minute
    start = ASIA_OPEN_ALERT_HOUR * 60
    end = start + ASIA_OPEN_ALERT_WINDOW_MINUTES
    return start <= mins < end

# =============================
# SENTIMENT
# =============================

def get_crypto_fear_greed():
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

        m = re.search(r"Now\s+([A-Za-z ]+?)\s+(\d{1,3})\s+Yesterday", text, flags=re.I)
        if m:
            return {
                "value": float(m.group(2)),
                "description": m.group(1).strip().lower(),
                "source": "feargreedmeter.com",
            }

        m2 = re.search(r"Fear and Greed Index.*?Now\s+([A-Za-z ]+?)\s+(\d{1,3})", text, flags=re.I)
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
    score = 50

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

    score += 8 if sp_above_50dma else -8
    score += 12 if sp_above_200dma else -12

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
# MARKET DATA HELPERS
# =============================

def _calc_pct(latest, prev_close):
    if latest is None or prev_close in (None, 0):
        raise ValueError("Missing latest or previous close")
    latest = float(latest)
    prev_close = float(prev_close)
    pct_change = ((latest - prev_close) / prev_close) * 100
    return latest, pct_change

def _normalize_df(df):
    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return df

def _get_close_series(df):
    df = _normalize_df(df)
    if df.empty or "Close" not in df.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(df["Close"], errors="coerce").dropna()

def _get_from_fast_info(asset, ticker):
    try:
        fi = asset.fast_info
        latest = fi.get("lastPrice")
        prev_close = fi.get("previousClose")
        if latest is not None and prev_close not in (None, 0):
            return _calc_pct(latest, prev_close)
    except Exception as e:
        print(f"fast_info failed for {ticker}:", e)
    return None

def _get_from_info(asset, ticker):
    try:
        info = asset.info or {}
        latest = (
            info.get("regularMarketPrice")
            or info.get("currentPrice")
            or info.get("navPrice")
            or info.get("previousClose")
        )
        prev_close = (
            info.get("regularMarketPreviousClose")
            or info.get("previousClose")
        )
        if latest is not None and prev_close not in (None, 0):
            return _calc_pct(latest, prev_close)
    except Exception as e:
        print(f"info fallback failed for {ticker}:", e)
    return None

def _history_attempts():
    return [
        {"period": "7d", "interval": "1d"},
        {"period": "1mo", "interval": "1d"},
        {"period": "3mo", "interval": "1d"},
        {"period": "6mo", "interval": "1d"},
        {"period": "1y", "interval": "1d"},
    ]

def _download_history(ticker, period, interval):
    try:
        df = yf.download(
            ticker,
            period=period,
            interval=interval,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        return _normalize_df(df)
    except Exception as e:
        print(f"yf.download failed for {ticker} ({period}, {interval}):", e)
        return pd.DataFrame()

def _ticker_history(asset, ticker, period, interval):
    try:
        df = asset.history(
            period=period,
            interval=interval,
            auto_adjust=False,
        )
        return _normalize_df(df)
    except Exception as e:
        print(f"Ticker.history failed for {ticker} ({period}, {interval}):", e)
        return pd.DataFrame()

def _get_best_history(asset, ticker, min_points=2):
    for attempt in _history_attempts():
        period = attempt["period"]
        interval = attempt["interval"]

        df = _ticker_history(asset, ticker, period, interval)
        closes = _get_close_series(df)
        if len(closes) >= min_points:
            return closes

        df = _download_history(ticker, period, interval)
        closes = _get_close_series(df)
        if len(closes) >= min_points:
            return closes

    raise ValueError(f"Not enough price history for {ticker}")

def _get_from_history(asset, ticker):
    closes = _get_best_history(asset, ticker, min_points=2)
    latest = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])
    pct_change = ((latest - prev) / prev) * 100
    return latest, pct_change

def _get_quote_single(ticker):
    asset = yf.Ticker(ticker)

    result = _get_from_fast_info(asset, ticker)
    if result is not None:
        return result

    result = _get_from_info(asset, ticker)
    if result is not None:
        return result

    return _get_from_history(asset, ticker)

def get_last_price_and_change(ticker):
    tickers_to_try = [ticker] + TICKER_ALIASES.get(ticker, [])
    last_error = None

    for candidate in tickers_to_try:
        try:
            return _get_quote_single(candidate)
        except Exception as e:
            last_error = e
            print(f"Quote attempt failed for {ticker} via {candidate}: {e}")

    raise last_error if last_error else ValueError(f"Failed to fetch quote for {ticker}")

def get_indicator_snapshot(ticker, period="1y"):
    tickers_to_try = [ticker] + TICKER_ALIASES.get(ticker, [])
    last_error = None

    for candidate in tickers_to_try:
        try:
            asset = yf.Ticker(candidate)
            closes = _get_best_history(asset, candidate, min_points=50)

            if len(closes) < 50:
                raise ValueError(f"Not enough indicator history for {candidate}")

            current = float(closes.iloc[-1])
            sma50 = float(closes.tail(min(50, len(closes))).mean())
            sma200 = float(closes.tail(min(200, len(closes))).mean())
            peak = float(closes.max())
            drawdown = (current - peak) / peak * 100
            pct_to_200 = ((current / sma200) - 1.0) * 100 if sma200 else 0.0

            return {
                "ticker_used": candidate,
                "current": current,
                "sma50": sma50,
                "sma200": sma200,
                "drawdown": drawdown,
                "below50": current < sma50,
                "below200": current < sma200,
                "pct_to_200": pct_to_200,
            }
        except Exception as e:
            last_error = e
            print(f"Indicator snapshot failed for {ticker} via {candidate}: {e}")

    raise last_error if last_error else ValueError(f"Failed indicator snapshot for {ticker}")

def get_ratio_snapshot(numerator_ticker, denominator_ticker, period="1y"):
    num_asset = yf.Ticker(numerator_ticker)
    den_asset = yf.Ticker(denominator_ticker)

    num_closes = _get_best_history(num_asset, numerator_ticker, min_points=50)
    den_closes = _get_best_history(den_asset, denominator_ticker, min_points=50)

    df = pd.DataFrame({
        "num": num_closes,
        "den": den_closes,
    }).dropna()

    if len(df) < 50:
        raise ValueError("Not enough ratio history")

    ratio = df["num"] / df["den"]
    current = float(ratio.iloc[-1])
    sma50 = float(ratio.tail(min(50, len(ratio))).mean())
    sma200 = float(ratio.tail(min(200, len(ratio))).mean())
    peak = float(ratio.max())
    drawdown = (current - peak) / peak * 100
    pct_to_200 = ((current / sma200) - 1.0) * 100 if sma200 else 0.0

    return {
        "current": current,
        "sma50": sma50,
        "sma200": sma200,
        "drawdown": drawdown,
        "below50": current < sma50,
        "below200": current < sma200,
        "pct_to_200": pct_to_200,
    }

def get_ai_basket_snapshot():
    snapshots = {}
    for name, ticker in AI_BASKET.items():
        try:
            snapshots[name] = get_indicator_snapshot(ticker)
        except Exception as e:
            print(f"AI basket member failed for {ticker}: {e}")

    if not snapshots:
        return {
            "count": 0,
            "below50_pct": 0.0,
            "below200_pct": 0.0,
            "avg_drawdown": 0.0,
            "avg_pct_to_200": 0.0,
            "members": {},
        }

    count = len(snapshots)
    below50_pct = sum(1 for s in snapshots.values() if s["below50"]) / count
    below200_pct = sum(1 for s in snapshots.values() if s["below200"]) / count
    avg_drawdown = sum(s["drawdown"] for s in snapshots.values()) / count
    avg_pct_to_200 = sum(s["pct_to_200"] for s in snapshots.values()) / count

    return {
        "count": count,
        "below50_pct": below50_pct,
        "below200_pct": below200_pct,
        "avg_drawdown": avg_drawdown,
        "avg_pct_to_200": avg_pct_to_200,
        "members": snapshots,
    }

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

    spy_snapshot = get_indicator_snapshot(SPY_TICKER)
    qqq_snapshot = get_indicator_snapshot(QQQ_TICKER)
    hyg_snapshot = get_indicator_snapshot(HYG_TICKER)
    credit_proxy = get_ratio_snapshot(HYG_TICKER, IEF_TICKER)
    ai_basket = get_ai_basket_snapshot()

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
        "spy_snapshot": spy_snapshot,
        "qqq_snapshot": qqq_snapshot,
        "hyg_snapshot": hyg_snapshot,
        "credit_proxy": credit_proxy,
        "ai_basket": ai_basket,
    }

# =============================
# SCORE BREAKDOWNS
# =============================

def get_market_risk_breakdown(data):
    score = 0
    lines = []

    vix = data["vix_price"]
    if vix >= 40:
        pts = 25
    elif vix >= 35:
        pts = 22
    elif vix >= 30:
        pts = 18
    elif vix >= 25:
        pts = 14
    elif vix >= 20:
        pts = 8
    elif vix >= 16:
        pts = 3
    else:
        pts = 0
    score += pts
    lines.append(f"VIX stress: +{pts}/25")

    dd = data["drawdown"]
    if dd <= -20:
        pts = 15
    elif dd <= -15:
        pts = 13
    elif dd <= -10:
        pts = 10
    elif dd <= -6:
        pts = 7
    elif dd <= -3:
        pts = 4
    else:
        pts = 0
    score += pts
    lines.append(f"S&P drawdown stress: +{pts}/15")

    stock_fg = data.get("stock_fear_greed")
    fg = stock_fg["value"] if stock_fg else None
    if fg is None:
        pts = 7
    elif fg <= 20:
        pts = 15
    elif fg <= 30:
        pts = 12
    elif fg <= 40:
        pts = 9
    elif fg <= 50:
        pts = 5
    elif fg <= 60:
        pts = 2
    else:
        pts = 0
    score += pts
    lines.append(f"Fear & Greed stress: +{pts}/15")

    spy = data["spy_snapshot"]
    qqq = data["qqq_snapshot"]
    pts = 0
    if spy["below50"]:
        pts += 3
    if spy["below200"]:
        pts += 4
    if qqq["below50"]:
        pts += 3
    if qqq["below200"]:
        pts += 5
    pts = min(15, pts)
    score += pts
    lines.append(f"SPY / QQQ trend damage: +{pts}/15")

    credit = data["credit_proxy"]
    hyg = data["hyg_snapshot"]
    pts = 0
    if hyg["below50"]:
        pts += 3
    if hyg["below200"]:
        pts += 4
    if credit["below50"]:
        pts += 3
    if credit["below200"]:
        pts += 4
    if credit["pct_to_200"] <= -3:
        pts += 1
    pts = min(15, pts)
    score += pts
    lines.append(f"Credit proxy stress (HYG / IEF): +{pts}/15")

    ai = data["ai_basket"]
    pts = 0
    if ai["count"] == 0:
        pts = 5
    else:
        if ai["below200_pct"] >= 0.75:
            pts += 8
        elif ai["below200_pct"] >= 0.50:
            pts += 6
        elif ai["below200_pct"] >= 0.25:
            pts += 3

        if ai["avg_drawdown"] <= -25:
            pts += 7
        elif ai["avg_drawdown"] <= -15:
            pts += 5
        elif ai["avg_drawdown"] <= -8:
            pts += 3
    pts = min(15, pts)
    score += pts
    lines.append(f"AI basket stress: +{pts}/15")

    return clamp_score(score), lines

def get_buy_opportunity_breakdown(data):
    raw = 0
    penalty = 0
    lines = []

    vix = data["vix_price"]
    if vix >= 40:
        pts = 16
    elif vix >= 35:
        pts = 14
    elif vix >= 30:
        pts = 12
    elif vix >= 25:
        pts = 9
    elif vix >= 20:
        pts = 5
    else:
        pts = 0
    raw += pts
    lines.append(f"VIX dislocation: +{pts}/16")

    dd = data["drawdown"]
    if dd <= -20:
        pts = 22
    elif dd <= -15:
        pts = 19
    elif dd <= -10:
        pts = 15
    elif dd <= -6:
        pts = 10
    elif dd <= -3:
        pts = 5
    else:
        pts = 0
    raw += pts
    lines.append(f"S&P drawdown reset: +{pts}/22")

    stock_fg = data.get("stock_fear_greed")
    fg = stock_fg["value"] if stock_fg else None
    if fg is None:
        pts = 8
    elif fg <= 15:
        pts = 22
    elif fg <= 25:
        pts = 18
    elif fg <= 35:
        pts = 14
    elif fg <= 45:
        pts = 8
    else:
        pts = 0
    raw += pts
    lines.append(f"Panic / fear setup: +{pts}/22")

    spy = data["spy_snapshot"]
    qqq = data["qqq_snapshot"]
    pts = 0
    if spy["below200"]:
        pts += 5
    elif spy["below50"]:
        pts += 2

    if qqq["below200"]:
        pts += 7
    elif qqq["below50"]:
        pts += 3

    if qqq["drawdown"] <= -12:
        pts += 2

    pts = min(14, pts)
    raw += pts
    lines.append(f"SPY / QQQ technical reset: +{pts}/14")

    ai = data["ai_basket"]
    pts = 0
    if ai["count"] > 0:
        if ai["avg_drawdown"] <= -30:
            pts += 10
        elif ai["avg_drawdown"] <= -20:
            pts += 8
        elif ai["avg_drawdown"] <= -12:
            pts += 5

        if ai["below200_pct"] >= 0.75:
            pts += 8
        elif ai["below200_pct"] >= 0.50:
            pts += 6
        elif ai["below200_pct"] >= 0.25:
            pts += 3
    pts = min(18, pts)
    raw += pts
    lines.append(f"AI basket reset: +{pts}/18")

    credit = data["credit_proxy"]
    hyg = data["hyg_snapshot"]

    pts = 0
    if credit["below200"]:
        pts += 3
    if credit["pct_to_200"] <= -1.5:
        pts += 2
    if hyg["below200"]:
        pts += 3
    pts = min(8, pts)
    raw += pts
    lines.append(f"Moderate credit stress: +{pts}/8")

    if credit["pct_to_200"] <= -6:
        penalty += 8
    elif credit["pct_to_200"] <= -4:
        penalty += 5
    elif credit["pct_to_200"] <= -3:
        penalty += 3

    if hyg["drawdown"] <= -12:
        penalty += 4
    elif hyg["drawdown"] <= -8:
        penalty += 2

    if vix >= 45 and dd <= -20:
        penalty += 4

    penalty = min(12, penalty)
    lines.append(f"Crash / credit penalty: -{penalty}/12")

    final_score = clamp_score(raw - penalty)
    return final_score, lines

def get_market_risk_score(data):
    score, _ = get_market_risk_breakdown(data)
    return score

def get_buy_opportunity_score(data):
    score, _ = get_buy_opportunity_breakdown(data)
    return score

def format_signal_scores(data):
    risk_score, risk_lines = get_market_risk_breakdown(data)
    buy_score, buy_lines = get_buy_opportunity_breakdown(data)

    lines = [
        "📐 Market Scores",
        f"Risk Level: {score_band(risk_score)} ({risk_score}/100)",
        f"Buy Opportunity: {score_band(buy_score)} ({buy_score}/100)",
        "",
        "Risk Score Breakdown",
        *[f"• {line}" for line in risk_lines],
        "",
        "Buy Opportunity Breakdown",
        *[f"• {line}" for line in buy_lines],
        "",
        "How to read it:",
        "• Higher risk = market conditions are more stressed",
        "• Higher buy opportunity = better phased long-term contrarian setup",
        "• Buy opportunity is not a short-term price forecast",
    ]
    return "\n".join(lines)

def format_score_method():
    return "\n".join([
        "🧠 Score Method",
        "",
        "Risk Score (0-100) adds:",
        "• VIX stress: max 25",
        "• S&P drawdown from 1Y high: max 15",
        "• Stock Fear & Greed stress: max 15",
        "• SPY / QQQ trend damage: max 15",
        "• Credit proxy stress (HYG / IEF + HYG trend): max 15",
        "• AI basket stress: max 15",
        "",
        "Buy Opportunity Score (0-100) adds:",
        "• VIX dislocation: max 16",
        "• S&P drawdown reset: max 22",
        "• Panic / fear setup: max 22",
        "• SPY / QQQ technical reset: max 14",
        "• AI basket reset: max 18",
        "• Moderate credit stress: max 8",
        "",
        "Then subtracts:",
        "• Crash / credit penalty: up to 12",
        "",
        "Key difference:",
        "• Risk asks: how dangerous / stressed is the market?",
        "• Buy Opportunity asks: how attractive is this for gradual long-term buying?",
        "",
        "Credit proxy note:",
        "• This uses HYG / IEF as a proxy for credit stress because it is easier to pull reliably from market data feeds than raw option-adjusted spreads.",
    ])

# =============================
# FORMATTERS
# =============================

def format_quote(name, ticker):
    price, pct = get_last_price_and_change(ticker)
    return f"{name}: {price:,.2f} ({pct:+.2f}%)"

def format_watchlist(title, watchlist):
    lines = [title]
    for name, ticker in watchlist.items():
        try:
            lines.append(format_quote(name, ticker))
        except Exception as e:
            lines.append(f"{name}: error ({e})")
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
    lines = ["🌏 Asia Markets", "", "Japan:"]
    for name, ticker in JAPAN_MARKETS.items():
        try:
            lines.append(f"  {format_quote(name, ticker)}")
        except Exception as e:
            lines.append(f"  {name}: error ({e})")

    lines.append("")
    lines.append("Korea:")
    for name, ticker in KOREA_MARKETS.items():
        try:
            lines.append(f"  {format_quote(name, ticker)}")
        except Exception as e:
            lines.append(f"  {name}: error ({e})")

    return "\n".join(lines)

def format_market_snapshot(data):
    risk_score = get_market_risk_score(data)
    buy_score = get_buy_opportunity_score(data)

    lines = [
        "📊 Market Snapshot",
        "",
        f"VIX: {data['vix_price']:.2f} ({data['vix_change']:+.2f}%)",
        f"S&P 500: {data['sp_current']:.2f}",
        f"Drawdown from 1Y peak: {data['drawdown']:.2f}%",
        f"Regime: {detect_market_regime(data)}",
        f"Risk Level: {score_band(risk_score)} ({risk_score}/100)",
        f"Buy Opportunity: {score_band(buy_score)} ({buy_score}/100)",
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

def build_combined_market_update(title, data):
    lines = [title, ""]
    lines.append(format_market_snapshot(data))
    lines.append("")
    lines.append(format_futures())
    lines.append("")
    lines.append(format_portfolio_watchlist())
    lines.append("")
    lines.append(format_crypto_prices())

    message = "\n".join(lines).strip()
    if len(message) > 3900:
        message = message[:3900].rstrip() + "\n\n...[truncated]"
    return message

def build_asia_open_update():
    lines = ["🔔 Asia Market Open", ""]
    lines.append(format_asia_markets())
    message = "\n".join(lines).strip()

    if len(message) > 3900:
        message = message[:3900].rstrip() + "\n\n...[truncated]"
    return message

# =============================
# MARKET REGIME
# =============================

def detect_market_regime(data):
    risk_score = get_market_risk_score(data)

    if risk_score >= 80 and data["drawdown"] <= -10:
        return "CRISIS"
    if risk_score >= 60:
        return "RISK OFF"
    if risk_score <= 30 and data["vix_price"] < 18:
        return "RISK ON"
    return "NEUTRAL"

def maybe_send_regime_alert(data):
    regime = detect_market_regime(data)
    risk_score = get_market_risk_score(data)
    buy_score = get_buy_opportunity_score(data)

    if regime != state.get("last_regime"):
        msg = (
            "🌎 MARKET REGIME SHIFT\n\n"
            f"Regime: {regime}\n"
            f"VIX: {data['vix_price']:.2f}\n"
            f"Drawdown: {data['drawdown']:.2f}%\n"
            f"Risk Level: {score_band(risk_score)} ({risk_score}/100)\n"
            f"Buy Opportunity: {score_band(buy_score)} ({buy_score}/100)"
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

def maybe_send_panic_alert(data):
    if panic_signal_triggered(data):
        now_ts = time.time()

        if now_ts - state.get("last_panic_alert_time", 0) > PANIC_ALERT_COOLDOWN_SECONDS:
            stock_fg = data.get("stock_fear_greed")
            fg_text = (
                f"{int(round(stock_fg['value']))} ({stock_fg['description']})"
                if stock_fg else "N/A"
            )

            risk_score = get_market_risk_score(data)
            buy_score = get_buy_opportunity_score(data)

            msg = (
                "🚨 STOCK PANIC ALERT 🚨\n\n"
                f"VIX: {data['vix_price']:.2f}\n"
                f"Stock Fear & Greed: {fg_text}\n"
                f"Drawdown: {data['drawdown']:.2f}%\n"
                f"Risk Level: {score_band(risk_score)} ({risk_score}/100)\n"
                f"Buy Opportunity: {score_band(buy_score)} ({buy_score}/100)"
            )

            if send_telegram_message(msg):
                state["last_panic_alert_time"] = now_ts
                save_state()

# =============================
# BUY ZONE
# =============================

def check_buy_zone(data):
    buy_score = get_buy_opportunity_score(data)
    active = buy_score >= 65
    was_active = bool(state.get("last_buy_zone_active", False))

    if active and not was_active:
        msg = (
            "🟢 BUY ZONE DETECTED\n\n"
            f"Buy Opportunity: {score_band(buy_score)} ({buy_score}/100)\n"
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
        today = datetime.now(ET_TZ).date().isoformat()

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
# US OPEN / FUTURES ALERTS
# =============================

def maybe_send_us_open_snapshot(now_et, data):
    today = now_et.date().isoformat()

    if not in_us_open_alert_window(now_et):
        return

    if state.get("last_us_open_alert_date") == today:
        return

    msg = build_combined_market_update("🔔 U.S. Market Open", data)
    if send_telegram_message(msg):
        state["last_us_open_alert_date"] = today
        save_state()

def maybe_send_us_futures_snapshot(now_et, data):
    if not is_us_futures_open(now_et):
        return

    bucket = get_futures_bucket(now_et)

    if state.get("last_us_futures_update_bucket") == bucket:
        return

    msg = build_combined_market_update("🕒 U.S. Futures Update", data)
    if send_telegram_message(msg):
        state["last_us_futures_update_bucket"] = bucket
        save_state()

# =============================
# ASIA OPEN ALERT
# =============================

def maybe_send_asia_open_snapshot():
    now_asia = datetime.now(ASIA_TZ)
    today = now_asia.date().isoformat()

    if not in_asia_open_alert_window(now_asia):
        return

    if state.get("last_asia_open_alert_date") == today:
        return

    msg = build_asia_open_update()
    if send_telegram_message(msg):
        state["last_asia_open_alert_date"] = today
        save_state()

# =============================
# NEWSLETTER
# =============================

def truncate_text(text, max_len=NEWSLETTER_MAX_HEADLINE_LEN):
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."

def fetch_google_news_rss(query, top_n=2):
    try:
        url = (
            "https://news.google.com/rss/search?"
            f"q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
        )
        r = safe_request(url, headers={"User-Agent": "Mozilla/5.0"})
        root = ETXML.fromstring(r.content)

        items = []
        channel = root.find("channel")
        if channel is None:
            return []

        for item in channel.findall("item")[:top_n]:
            title = item.findtext("title", default="").strip()
            title = html.unescape(title)
            title = truncate_text(title)
            items.append({"title": title})

        return items
    except Exception as e:
        print(f"RSS fetch error for query '{query}':", e)
        return []

def get_newsletter_sections():
    return [
        ("🤖 AI", "artificial intelligence OR generative AI OR OpenAI OR Anthropic OR Nvidia"),
        ("📈 Investing", "stock market OR federal reserve OR treasury yields OR inflation OR recession"),
        ("💻 Technology", "technology OR semiconductors OR chips OR Microsoft OR Apple OR Google"),
        ("🌍 Global Politics", "global politics OR geopolitics OR diplomacy OR war OR election"),
        ("🇨🇳 China", "China economy OR China politics OR China technology OR China markets"),
    ]

def build_newsletter():
    data = get_market_data()
    risk_score = get_market_risk_score(data)
    buy_score = get_buy_opportunity_score(data)

    lines = []
    now_asia = datetime.now(ASIA_TZ).strftime("%Y-%m-%d %H:%M")
    lines.append(f"📰 Brian Daily Brief | {now_asia} JST")
    lines.append("")

    lines.append("📊 Markets")
    lines.append(f"VIX: {data['vix_price']:.2f} ({data['vix_change']:+.2f}%)")
    lines.append(f"S&P 500: {data['sp_current']:.2f}")
    lines.append(f"Drawdown: {data['drawdown']:.2f}%")
    lines.append(f"Regime: {detect_market_regime(data)}")
    lines.append(f"Risk Level: {score_band(risk_score)} ({risk_score}/100)")
    lines.append(f"Buy Opportunity: {score_band(buy_score)} ({buy_score}/100)")

    stock_fg = data.get("stock_fear_greed")
    if stock_fg:
        lines.append(
            f"Stock Fear & Greed: {int(round(stock_fg['value']))} ({stock_fg['description']})"
        )

    crypto_fg = data.get("crypto_fear_greed")
    if crypto_fg:
        lines.append(
            f"Crypto Fear & Greed: {int(round(crypto_fg['value']))} ({crypto_fg['description']})"
        )

    lines.append("")
    lines.append("📉 Futures")
    for name, ticker in FUTURES_WATCHLIST.items():
        try:
            price, pct = get_last_price_and_change(ticker)
            lines.append(f"{name}: {price:,.2f} ({pct:+.2f}%)")
        except Exception as e:
            lines.append(f"{name}: error ({e})")

    lines.append("")
    lines.append("📁 Your Watchlist")
    for name, ticker in PORTFOLIO_WATCHLIST.items():
        try:
            price, pct = get_last_price_and_change(ticker)
            lines.append(f"{name}: {price:,.2f} ({pct:+.2f}%)")
        except Exception as e:
            lines.append(f"{name}: error ({e})")

    lines.append("")
    lines.append("💰 Crypto")
    for name, ticker in CRYPTO_WATCHLIST.items():
        try:
            price, pct = get_last_price_and_change(ticker)
            lines.append(f"{name}: {price:,.2f} ({pct:+.2f}%)")
        except Exception as e:
            lines.append(f"{name}: error ({e})")

    lines.append("")
    lines.append("🌏 Asia")
    for name, ticker in {**JAPAN_MARKETS, **KOREA_MARKETS}.items():
        try:
            price, pct = get_last_price_and_change(ticker)
            lines.append(f"{name}: {price:,.2f} ({pct:+.2f}%)")
        except Exception as e:
            lines.append(f"{name}: error ({e})")

    lines.append("")
    for section_name, query in get_newsletter_sections():
        lines.append(section_name)
        headlines = fetch_google_news_rss(query, top_n=NEWSLETTER_TOP_N)

        if not headlines:
            lines.append("No update.")
        else:
            for item in headlines:
                lines.append(f"• {item['title']}")
        lines.append("")

    message = "\n".join(lines).strip()

    if len(message) > 3900:
        message = message[:3900].rstrip() + "\n\n...[truncated]"

    return message

def maybe_send_daily_newsletter():
    now_asia = datetime.now(ASIA_TZ)
    today = now_asia.date().isoformat()

    if now_asia.hour != NEWSLETTER_HOUR_ASIA:
        return

    if not (NEWSLETTER_MINUTE_ASIA <= now_asia.minute < NEWSLETTER_MINUTE_ASIA + 5):
        return

    if state.get("last_newsletter_date") == today:
        return

    msg = build_newsletter()
    if send_telegram_message(msg):
        state["last_newsletter_date"] = today
        save_state()

# =============================
# COMMANDS
# =============================

def handle_command(text):
    text = text.strip()
    if not text:
        return None

    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]

    if not cmd.startswith("/"):
        cmd = "/" + cmd

    try:
        if cmd in ("/start", "/help"):
            return (
                "📘 Commands\n\n"
                "/price - market snapshot\n"
                "/score - score + breakdown\n"
                "/scoremethod - explain score formula\n"
                "/portfolio - your ticker watchlist\n"
                "/quote NVDA - quote any ticker\n"
                "/crypto - BTC / ETH / XRP\n"
                "/futures - Dow / Nasdaq / S&P futures\n"
                "/vix - VIX check\n"
                "/oil - oil price\n"
                "/bond - 10Y yield\n"
                "/regime - market regime explanation\n"
                "/panic - panic signal status\n"
                "/sentiment - stock + crypto fear/greed\n"
                "/japan - Nikkei + TOPIX\n"
                "/korea - KOSPI + KOSDAQ\n"
                "/asia - Japan + Korea snapshot\n"
                "/asiaopenupdate - send Asia open snapshot now\n"
                "/newsletter - full daily briefing\n"
                "/openupdate - send combined U.S. open snapshot now\n"
                "/futuresupdate - send combined futures snapshot now\n"
                "/help - command list"
            )

        if cmd == "/price":
            data = get_market_data()
            return format_market_snapshot(data)

        if cmd == "/score":
            data = get_market_data()
            return format_signal_scores(data)

        if cmd == "/scoremethod":
            return format_score_method()

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
            regime = detect_market_regime(data)
            return (
                f"🌎 Regime: {regime}\n\n"
                "RISK OFF = stressed / defensive market\n"
                "NEUTRAL = mixed conditions\n"
                "RISK ON = calmer / supportive market\n"
                "CRISIS = extreme stress\n\n"
                + format_market_snapshot(data)
            )

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

        if cmd == "/asiaopenupdate":
            return build_asia_open_update()

        if cmd == "/newsletter":
            return build_newsletter()

        if cmd == "/openupdate":
            data = get_market_data()
            return build_combined_market_update("🔔 U.S. Market Open", data)

        if cmd == "/futuresupdate":
            data = get_market_data()
            return build_combined_market_update("🕒 U.S. Futures Update", data)

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
            check_telegram_commands()

            now_ts = time.time()
            if now_ts >= next_market_check:
                now_et = datetime.now(ET_TZ)

                data = get_market_data()

                maybe_send_us_open_snapshot(now_et, data)
                maybe_send_us_futures_snapshot(now_et, data)
                maybe_send_asia_open_snapshot()
                maybe_send_panic_alert(data)
                maybe_send_regime_alert(data)
                check_buy_zone(data)
                check_oil_spike()
                check_bond_spike()
                maybe_send_daily_newsletter()

                next_market_check = now_ts + MARKET_CHECK_SECONDS

            time.sleep(TELEGRAM_POLL_SECONDS)

        except Exception as e:
            print("Main loop error:", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
