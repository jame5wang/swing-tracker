"""
抓取台灣證交所「上市股票」盤後收盤行情，寫入 data/prices.json
資料來源：openapi.twse.com.tw（官方免費 OpenAPI，僅涵蓋「上市」股票，不含上櫃）

日期欄位說明：
- 每筆資料的 "date" 取自 API 回傳的 Date 欄位（民國年 1150929 → 2026-09-29），
  代表「這筆收盤價是哪一個交易日的」，而不是腳本執行的日期。
- 檔案最上層的 "date" 取所有資料列中最新的交易日；"fetchedAt" 才是實際抓取時間。
- 過去直接把執行日當成 date，導致連假期間（9/25~9/28 休市）的舊收盤價被標成新日期，
  網站上看起來像是最新資料，其實是好幾天前的價格。
- 若 API 沒有提供 Date 欄位，退回用執行日並標記 dateSource="runDate"，
  之後的 reconcile_prices.py 會再用個股逐日K資料校正追蹤中的股票。
"""
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta

URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
OUTPUT_PATH = "data/prices.json"
RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY = 8  # 秒，線性遞增：8, 16, 24, 32...
TZ = timezone(timedelta(hours=8))


def roc_compact_to_iso(value):
    """'1150929' 或 '115/09/29'（民國年）→ '2026-09-29'；無法解析回傳 None"""
    if value is None:
        return None
    s = str(value).strip().replace("/", "").replace("-", "")
    if not s.isdigit() or len(s) not in (7, 8):
        return None
    if len(s) == 8:  # 已經是西元 YYYYMMDD
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:])
    else:
        y, m, d = int(s[:3]) + 1911, int(s[3:5]), int(s[5:])
    try:
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def fetch_twse_raw():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_with_retry():
    """證交所 API 偶爾會暫時連不上或逾時，單次失敗不代表整天沒資料，重試幾次。"""
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            raw = fetch_twse_raw()
            if raw:
                return raw
            last_exc = RuntimeError("回傳資料為空")
        except Exception as e:  # 包含 TimeoutError / IncompleteRead 等非 URLError 的例外
            last_exc = e
        print(f"第{attempt}次嘗試失敗：{last_exc}")
        if attempt < RETRY_ATTEMPTS:
            delay = RETRY_BASE_DELAY * attempt
            print(f"{delay}秒後重試...")
            time.sleep(delay)
    raise last_exc


def mark_stale(reason):
    """重試多次仍失敗時，把舊資料標記為 stale，讓前端能明確提示「資料非最新」。"""
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    old["stale"] = True
    old["staleReason"] = reason
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, separators=(",", ":"))


def main():
    try:
        raw = fetch_with_retry()
    except Exception as e:
        print(f"::warning::上市收盤價重試{RETRY_ATTEMPTS}次後仍失敗，保留舊資料並標記為 stale：{e}")
        mark_stale(f"抓取失敗：{e}")
        return

    now = datetime.now(TZ)
    stocks = []
    row_dates = []
    for row in raw:
        row_date = roc_compact_to_iso(row.get("Date"))
        if row_date:
            row_dates.append(row_date)
        stocks.append({
            "code": row.get("Code"),
            "name": row.get("Name"),
            "date": row_date,
            "close": row.get("ClosingPrice"),
            "open": row.get("OpeningPrice"),
            "high": row.get("HighestPrice"),
            "low": row.get("LowestPrice"),
            "volume": row.get("TradeVolume"),
            "change": row.get("Change"),  # 今日漲跌（帶正負號），前端用 close-change 算昨收
        })

    if row_dates:
        data_date = max(row_dates)
        date_source = "api"
    else:
        data_date = now.strftime("%Y-%m-%d")
        date_source = "runDate"
        print("::notice::API 回傳資料沒有 Date 欄位，暫以執行日期標記，追蹤股票會再由 reconcile_prices.py 校正")

    output = {
        "date": data_date,
        "dateSource": date_source,
        "fetchedAt": now.isoformat(timespec="seconds"),
        "source": "TWSE OpenAPI (上市)",
        "stale": False,
        "stocks": stocks,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"寫入 {len(stocks)} 檔股票資料，資料交易日：{data_date}（{date_source}），抓取時間：{output['fetchedAt']}")


if __name__ == "__main__":
    main()
