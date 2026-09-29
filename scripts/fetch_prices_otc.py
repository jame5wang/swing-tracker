"""
抓取櫃買中心（TPEx）「上櫃股票」盤後收盤行情，寫入 data/prices_otc.json

注意：TPEx OpenAPI 有防機器人機制，本腳本用「多重欄位名稱嘗試」的方式盡量兼容，
若欄位對不上，會在執行紀錄（GitHub Actions log）印出原始資料的前幾筆，方便除錯調整。
"""
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta

URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
OUTPUT_PATH = "data/prices_otc.json"
RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY = 8  # 秒，線性遞增：8, 16, 24, 32...

# 每個欄位可能出現的多種 key 名稱，依序嘗試
FIELD_CANDIDATES = {
    "code": ["Code", "代號", "SecuritiesCompanyCode", "CompanyCode"],
    "name": ["Name", "名稱", "CompanyName"],
    "close": ["Close", "收盤", "ClosingPrice", "收盤價"],
    "open": ["Open", "開盤", "OpeningPrice", "開盤價"],
    "high": ["High", "最高", "HighestPrice", "最高價"],
    "low": ["Low", "最低", "LowestPrice", "最低價"],
    "volume": ["TradingShares", "成交股數", "TradeVolume", "成交量"],
    "change": ["Change", "漲跌", "漲跌值", "漲跌價"],
}


def pick(row, keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def fetch_tpex_raw():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_with_retry():
    """櫃買中心 API 偶爾會暫時連不上或逾時，用重試機制盡量在單次執行內就抓到最新資料，
    不要一次失敗就整天沒資料——之前發生過重試沒做、失敗一次就沿用前一天舊價格，
    導致個股買賣訊號判斷用到過期股價的情況。"""
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            raw = fetch_tpex_raw()
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
    """重試多次仍失敗時，把舊資料標記為 stale，讓前端能明確提示「資料非最新」，
    而不是悄悄沿用舊價格卻讓使用者以為是今天的收盤價。"""
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

    print("原始資料範例（前 2 筆，供除錯）：")
    print(json.dumps(raw[:2], ensure_ascii=False, indent=2))

    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).strftime("%Y-%m-%d")

    stocks = []
    for row in raw:
        code = pick(row, FIELD_CANDIDATES["code"])
        if not code:
            continue
        stocks.append({
            "code": str(code).strip(),
            "name": pick(row, FIELD_CANDIDATES["name"]),
            "close": pick(row, FIELD_CANDIDATES["close"]),
            "open": pick(row, FIELD_CANDIDATES["open"]),
            "high": pick(row, FIELD_CANDIDATES["high"]),
            "low": pick(row, FIELD_CANDIDATES["low"]),
            "volume": pick(row, FIELD_CANDIDATES["volume"]),
            "change": pick(row, FIELD_CANDIDATES["change"]),  # 今日漲跌（帶正負號），前端用 close-change 算昨收
        })

    output = {"date": today, "source": "TPEx OpenAPI (上櫃)", "stale": False, "stocks": stocks}

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"寫入 {len(stocks)} 檔股票資料，日期：{today}")


if __name__ == "__main__":
    main()
