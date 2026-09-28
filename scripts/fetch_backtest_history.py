"""
一次性抓取所有追蹤股票近13個月的完整日K（開高低收），供回測引擎使用。
與 fetch_technical.py 邏輯相同（STOCK_DAY / tradingStock），但存更長歷史、
不做技術指標計算，只單純輸出逐日OHLC，寫入 data/backtest_history.json。

這個檔案只給回測用，不是給前端網頁顯示用。
"""
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

TWSE_STOCK_DAY = "https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date}&stockNo={code}"
TPEX_TRADING_STOCK = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?date={date}&code={code}&response=json"
OUTPUT_PATH = "data/backtest_history.json"
MONTHS = 13  # 涵蓋今年以來(1月起)還留有60個交易日的前置緩衝可以計算機械化支撐壓力

# 與 fetch_technical.py 的 TWSE_CODES / OTC_CODES 一致（含第六批新增 2345 / 6510）
TWSE_CODES = [
    "3037", "8046", "3189", "2383", "2368", "4958", "2313", "3711", "6239", "2303",
    "2344", "2408", "3481", "2308", "2301", "2455", "6213", "6672",
    "3017", "3653", "3167", "8021",
    "6442", "3443", "3661", "3008", "2454", "7610", "7750", "2464", "4576", "2049",
    "8996", "6451", "8039", "2327", "3026", "1303", "7788", "2360", "6515", "2449", "6531",
    "7769", "3533", "6805", "3532", "6278",
    "2345",
]
OTC_CODES = [
    "6274", "8358", "6182", "6488", "6147", "8299",
    "5289", "3105", "4971",
    "3324", "3529", "6187", "6739",
    "3081", "3363", "3163", "6727", "6922",
    "4979", "6223", "6683", "7828",
    "6510",
]


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def roc_date_to_iso(date_roc):
    y, m, d = str(date_roc).strip().split("/")
    return f"{int(y)+1911}-{int(m):02d}-{int(d):02d}"


def fetch_stock_day_months(code, months):
    rows = []
    today = datetime.now(timezone(timedelta(hours=8)))
    for i in range(months):
        year = today.year
        month = today.month - i
        while month <= 0:
            month += 12
            year -= 1
        date_str = f"{year}{month:02d}01"
        url = TWSE_STOCK_DAY.format(date=date_str, code=code)
        try:
            raw = http_get_json(url)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            continue
        data = raw.get("data") or []
        for row in data:
            try:
                date_iso = roc_date_to_iso(row[0])
                open_p = float(str(row[3]).replace(",", ""))
                high = float(str(row[4]).replace(",", ""))
                low = float(str(row[5]).replace(",", ""))
                close = float(str(row[6]).replace(",", ""))
                rows.append({"date": date_iso, "open": open_p, "high": high, "low": low, "close": close})
            except (ValueError, IndexError):
                continue
    seen = {}
    for r in rows:
        seen[r["date"]] = r
    return sorted(seen.values(), key=lambda r: r["date"])


def fetch_otc_stock_months(code, months):
    rows = []
    today = datetime.now(timezone(timedelta(hours=8)))
    for i in range(months):
        year = today.year
        month = today.month - i
        while month <= 0:
            month += 12
            year -= 1
        date_str = f"{year}/{month:02d}/01"
        url = TPEX_TRADING_STOCK.format(date=date_str, code=code)
        try:
            raw = http_get_json(url)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            continue
        tables = raw.get("tables") or []
        if not tables:
            continue
        for row in tables[0].get("data") or []:
            try:
                date_roc = str(row[0]).replace("*", "").strip()
                open_p, high_p, low_p, close_p = row[3], row[4], row[5], row[6]
                if "--" in (str(open_p), str(high_p), str(low_p), str(close_p)):
                    continue
                open_v = float(str(open_p).replace(",", ""))
                high = float(str(high_p).replace(",", ""))
                low = float(str(low_p).replace(",", ""))
                close = float(str(close_p).replace(",", ""))
                y, m, d = date_roc.split("/")
                date_iso = f"{int(y)+1911}-{int(m):02d}-{int(d):02d}"
                rows.append({"date": date_iso, "open": open_v, "high": high, "low": low, "close": close})
            except (ValueError, IndexError, TypeError):
                continue
    seen = {}
    for r in rows:
        seen[r["date"]] = r
    return sorted(seen.values(), key=lambda r: r["date"])


def main():
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).strftime("%Y-%m-%d")

    result = {}
    for code in TWSE_CODES:
        rows = fetch_stock_day_months(code, months=MONTHS)
        if rows:
            result[code] = rows
        print(f"TWSE {code}: {len(rows)} days")

    for code in OTC_CODES:
        rows = fetch_otc_stock_months(code, months=MONTHS)
        if rows:
            result[code] = rows
        print(f"OTC {code}: {len(rows)} days")

    output = {
        "generated": today,
        "note": f"近{MONTHS}個月完整日K，供回測引擎使用（非前端顯示用）",
        "stocks": result,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))
    print(f"寫入 {len(result)} 檔股票歷史資料")


if __name__ == "__main__":
    main()
