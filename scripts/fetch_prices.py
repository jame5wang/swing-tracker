"""
抓取台灣證交所「上市股票」盤後收盤行情，寫入 data/prices.json
資料來源：openapi.twse.com.tw（官方免費 OpenAPI，僅涵蓋「上市」股票，不含上櫃）
"""
import json
import urllib.request
from datetime import datetime, timezone, timedelta

URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
OUTPUT_PATH = "data/prices.json"


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.load(resp)

    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).strftime("%Y-%m-%d")

    stocks = []
    for row in raw:
        stocks.append({
            "code": row.get("Code"),
            "name": row.get("Name"),
            "close": row.get("ClosingPrice"),
            "open": row.get("OpeningPrice"),
            "high": row.get("HighestPrice"),
            "low": row.get("LowestPrice"),
            "volume": row.get("TradeVolume"),
            "change": row.get("Change"),  # 今日漲跌（帶正負號），前端用 close-change 算昨收
        })

    output = {"date": today, "source": "TWSE OpenAPI (上市)", "stocks": stocks}

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"寫入 {len(stocks)} 檔股票資料，日期：{today}")


if __name__ == "__main__":
    main()
