"""
抓取櫃買中心（TPEx）「上櫃股票」盤後收盤行情，寫入 data/prices_otc.json

注意：TPEx OpenAPI 有防機器人機制，本腳本用「多重欄位名稱嘗試」的方式盡量兼容，
若欄位對不上，會在執行紀錄（GitHub Actions log）印出原始資料的前幾筆，方便除錯調整。

日期欄位說明（與 fetch_prices.py 相同）：
- 每筆資料的 "date" 取自 API 回傳的 Date 欄位（民國年 1150929 → 2026-09-29），
  代表這筆收盤價屬於哪個交易日；檔案最上層 "date" 取最新交易日，"fetchedAt" 是抓取時間。
- API 沒有 Date 欄位時退回用執行日並標記 dateSource="runDate"。
"""
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta

URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
OUTPUT_PATH = "data/prices_otc.json"
RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY = 8  # 秒，線性遞增：8, 16, 24, 32...
TZ = timezone(timedelta(hours=8))

# 每個欄位可能出現的多種 key 名稱，依序嘗試
FIELD_CANDIDATES = {
    "code": ["Code", "代號", "SecuritiesCompanyCode", "CompanyCode"],
    "name": ["Name", "名稱", "CompanyName"],
    "date": ["Date", "日期", "資料日期"],
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


def roc_compact_to_iso(value):
    """'1150929' 或 '115/09/29'（民國年）→ '2026-09-29'；無法解析回傳 None"""
    if value is None:
        return None
    s = str(value).strip().replace("/", "").replace("-", "")
    if not s.isdigit() or len(s) not in (7, 8):
        return None
    if len(s) == 8:
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:])
    else:
        y, m, d = int(s[:3]) + 1911, int(s[3:5]), int(s[5:])
    try:
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def fetch_tpex_raw():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_with_retry():
    """櫃買中心 API 偶爾會暫時連不上或逾時，用重試機制盡量在單次執行內就抓到最新資料，
    不要一次失敗就整天沒資料——之前發生過沒有重試、失敗一次就沿用前一天舊價格，
    導致個股買賣訊號判斷用到過期股價的情況。"""
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            raw = fetch_tpex_raw()
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
    """重試多次仍失敗時，把舊資料標記為 stale，讓前端能明確提示「資料非最新」，
    而不是悄悄沿用舊價格卻讓使用者以為是今天的收盤價。"""
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
        print(f"::warning::上櫃收盤價重試{RETRY_ATTEMPTS}次後仍失敗，保留舊資料並標記為 stale：{e}")
        mark_stale(f"抓取失敗：{e}")
        return

    print("原始資料範例（前 2 筆，供除錯）：")
    print(json.dumps(raw[:2], ensure_ascii=False, indent=2))

    now = datetime.now(TZ)
    stocks = []
    row_dates = []
    for row in raw:
        code = pick(row, FIELD_CANDIDATES["code"])
        if not code:
            continue
        row_date = roc_compact_to_iso(pick(row, FIELD_CANDIDATES["date"]))
        if row_date:
            row_dates.append(row_date)
        stocks.append({
            "code": str(code).strip(),
            "name": pick(row, FIELD_CANDIDATES["name"]),
            "date": row_date,
            "close": pick(row, FIELD_CANDIDATES["close"]),
            "open": pick(row, FIELD_CANDIDATES["open"]),
            "high": pick(row, FIELD_CANDIDATES["high"]),
            "low": pick(row, FIELD_CANDIDATES["low"]),
            "volume": pick(row, FIELD_CANDIDATES["volume"]),
            "change": pick(row, FIELD_CANDIDATES["change"]),  # 今日漲跌（帶正負號），前端用 close-change 算昨收
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
        "source": "TPEx OpenAPI (上櫃)",
        "stale": False,
        "stocks": stocks,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"寫入 {len(stocks)} 檔股票資料，資料交易日：{data_date}（{date_source}），抓取時間：{output['fetchedAt']}")


if __name__ == "__main__":
    main()
