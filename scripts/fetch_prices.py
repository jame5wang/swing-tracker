"""
抓取台灣證交所「上市股票」盤後收盤行情，寫入 data/prices.json
資料來源：openapi.twse.com.tw（官方免費 OpenAPI，僅涵蓋「上市」股票，不含上櫃）
"""
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta

URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
OUTPUT_PATH = "data/prices.json"
RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY = 8  # 秒，線性遞增：8, 16, 24, 32...


def fetch_twse_raw():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_with_retry():
    """證交所 API 偶爾會暫時連不上或逾時，原本沒有重試機制，單次失敗整個步驟就直接中斷，
    當天完全沒有上市股價資料。改成重試機制，盡量在單次執行內就抓到最新資料。"""
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            raw = fetch_twse_raw()
            if raw:
                return raw
            last_exc = RuntimeError("回傳資料為空")
        except Exception as e:
            last_exc = e
        print(f"第{attempt}次嘗試失敗：{last_exc}")
        if attempt < RETRY_ATTEMPTS:
            delay = RETRY_BASE_DELAY * attempt
            print(f"{delay}秒後重試...")
            time.sleep(delay)
    raise last_exc


def mark_stale():
    """重試多次仍失敗時，把舊資料標記為 stale，讓前端能明確提示「資料非最新」。"""
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    old["stale"] = True
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, separators=(",", ":"))


def main():
    try:
        raw = fetch_with_retry()
    except Exception as e:
        print(f"重試{RETRY_ATTEMPTS}次後仍失敗，保留舊資料並標記為 stale：{e}")
        mark_stale()
        return

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

    output = {"date": today, "source": "TWSE OpenAPI (上市)", "stale": False, "stocks": stocks}

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"寫入 {len(stocks)} 檔股票資料，日期：{today}")


if __name__ == "__main__":
    main()
