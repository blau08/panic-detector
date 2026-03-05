import yfinance as yf
import requests
import time


def check_market():

    print("Checking markets...")

    # VIX
    vix = yf.Ticker("^VIX")
    vix_price = vix.history(period="1d")["Close"].iloc[-1]

    # S&P 500
    sp = yf.Ticker("^GSPC")
    sp_data = sp.history(period="1y")

    current = sp_data["Close"].iloc[-1]
    peak = sp_data["Close"].max()

    drawdown = (current - peak) / peak * 100

    # Fear & Greed
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    data = requests.get(url).json()
    fear_greed = data["fear_and_greed"]["score"]

    print("VIX:", vix_price)
    print("Fear & Greed:", fear_greed)
    print("Drawdown:", drawdown)

    if vix_price > 30 and fear_greed < 20 and drawdown < -20:
        print("🚨 PANIC BUY SIGNAL 🚨")


while True:
    check_market()
    print("Sleeping 1 hour...")
    time.sleep(3600)
