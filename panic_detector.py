import yfinance as yf
import requests
import time


def get_fear_greed():

    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

    try:
        response = requests.get(url, timeout=10)

        if response.status_code != 200:
            print("Fear & Greed API failed")
            return None

        data = response.json()
        return data["fear_and_greed"]["score"]

    except Exception as e:
        print("Fear & Greed fetch error:", e)
        return None


def check_market():

    print("Checking markets...")

    try:
        # VIX
        vix = yf.Ticker("^VIX")
        vix_price = vix.history(period="1d")["Close"].iloc[-1]

        # S&P500
        sp = yf.Ticker("^GSPC")
        sp_data = sp.history(period="1y")

        current = sp_data["Close"].iloc[-1]
        peak = sp_data["Close"].max()

        drawdown = (current - peak) / peak * 100

        # Fear & Greed
        fear_greed = get_fear_greed()

        print("VIX:", vix_price)
        print("Fear & Greed:", fear_greed)
        print("Drawdown:", drawdown)

        if fear_greed is not None:

            if vix_price > 30 and fear_greed < 20 and drawdown < -20:
                print("🚨 PANIC BUY SIGNAL 🚨")

    except Exception as e:
        print("Market check error:", e)


while True:

    check_market()

    print("Sleeping 1 hour...")
    time.sleep(3600)
