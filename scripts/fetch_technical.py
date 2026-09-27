"""
抓取追蹤股票的近期歷史股價，計算技術面參考指標，寫入 data/technical.json
- 上市股票：透過證交所 STOCK_DAY 逐月歷史資料，計算 MA10 / MA20 / 20日與60日高低點
- 上櫃股票：改用櫃買中心「個股日成交資訊」（tradingStock）逐月歷史資料，跟上市股票
  一樣每次直接抓近 4 個月，不再需要「每日累積」慢慢養資料——這支端點過去沒被
  找到，原本誤以為上櫃只有「今日快照」可用（tpex_mainboard_daily_close_quotes），
  所以改成每天存一筆累積；但那個端點其實只是另一個不同用途的快照 API，
  tradingStock 才是官方提供的個股歷史資料來源，一次就能拿到完整月份的
  開高低收，不用再等好幾週才能算出 MA20/60日高低點。
- 大盤：加權指數(TAIEX) 用證交所 FMTQIK，作為個股「相對強弱」比較基準。
        櫃買指數目前沒有找到穩定可用的官方逐日歷史 API，因此上櫃股票的相對強弱
        也先用加權指數做參考基準（非完全精確，但方向性仍有意義）。
"""
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

TWSE_STOCK_DAY = "https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date}&stockNo={code}"
TWSE_FMTQIK = "https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={date}"
TPEX_TRADING_STOCK = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?date={date}&code={code}&response=json"
OUTPUT_PATH = "data/technical.json"

# 與 index.html 的 SEED_STOCKS 一致；市場分類供本腳本抓取歷史資料使用
TWSE_CODES = [
    "3037", "8046", "3189", "2383", "2368", "4958", "2313", "3711", "6239", "2303",
    "2344", "2408", "3481", "2308", "2301", "2455", "6213", "6672",
]
OTC_CODES = [
    "6274", "8358", "6182", "6488", "6147", "8299",
    "5289", "3105", "4971",
]


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_stock_day_months(code, months):
    """抓取指定股票近 N 個月的 STOCK_DAY 資料，回傳依日期排序的 (date, close, high, low) list"""
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
                date_roc = row[0]
                close = float(str(row[6] if len(row) > 6 else row[-1]).replace(",", ""))
                high = float(str(row[4] if len(row) > 4 else row[-1]).replace(",", ""))
                low = float(str(row[5] if len(row) > 5 else row[-1]).replace(",", ""))
                rows.append((date_roc, close, high, low))
            except (ValueError, IndexError):
                continue
    # 去重、依日期排序（民國年字串排序在同世紀內是安全的）
    seen = {}
    for r in rows:
        seen[r[0]] = r
    ordered = sorted(seen.values(), key=lambda r: r[0])
    return ordered


def fetch_otc_stock_months(code, months):
    """抓取指定上櫃股票近 N 個月的個股日成交資訊，回傳依日期排序的 (date, close, high, low) list"""
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
                close = float(str(close_p).replace(",", ""))
                high = float(str(high_p).replace(",", ""))
                low = float(str(low_p).replace(",", ""))
                y, m, d = date_roc.split("/")
                date_key = f"{int(y)+1911}{int(m):02d}{int(d):02d}"
                rows.append((date_key, close, high, low))
            except (ValueError, IndexError, TypeError):
                continue
    seen = {}
    for r in rows:
        seen[r[0]] = r
    ordered = sorted(seen.values(), key=lambda r: r[0])
    return ordered


def compute_metrics(rows):
    """rows: list of (date, close, high, low)，由舊到新排序"""
    if not rows:
        return None
    closes = [r[1] for r in rows]
    highs = [r[2] for r in rows]
    lows = [r[3] for r in rows]
    n = len(closes)
    result = {
        "days": n,
        "insufficientHistory": n < 20,
        "lastClose": closes[-1],
    }
    if n >= 10:
        result["ma10"] = round(sum(closes[-10:]) / 10, 2)
    if n >= 20:
        result["ma20"] = round(sum(closes[-20:]) / 20, 2)
        result["high20"] = max(highs[-20:])
        result["low20"] = min(lows[-20:])
        result["change20d"] = round((closes[-1] - closes[-20]) / closes[-20] * 100, 2)
    if n >= 60:
        result["high60"] = max(highs[-60:])
        result["low60"] = min(lows[-60:])
    elif n >= 1:
        result["high60"] = max(highs)
        result["low60"] = min(lows)
    if n >= 5:
        result["change5d"] = round((closes[-1] - closes[-5]) / closes[-5] * 100, 2)
    return result


def main():
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).strftime("%Y-%m-%d")

    # 1) 大盤加權指數（TAIEX）
    taiex_rows = []
    now = datetime.now(tz)
    for i in range(3):
        year = now.year
        month = now.month - i
        while month <= 0:
            month += 12
            year -= 1
        date_str = f"{year}{month:02d}01"
        try:
            raw = http_get_json(TWSE_FMTQIK.format(date=date_str))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            continue
        for row in raw.get("data") or []:
            try:
                taiex_rows.append((row[0], float(str(row[4]).replace(",", ""))))
            except (ValueError, IndexError):
                continue
    seen = {}
    for r in taiex_rows:
        seen[r[0]] = r[1]
    taiex_closes = [seen[k] for k in sorted(seen.keys())]

    taiex_metrics = {}
    if len(taiex_closes) >= 5:
        taiex_metrics["change5d"] = round((taiex_closes[-1] - taiex_closes[-5]) / taiex_closes[-5] * 100, 2)
    if len(taiex_closes) >= 20:
        taiex_metrics["change20d"] = round((taiex_closes[-1] - taiex_closes[-20]) / taiex_closes[-20] * 100, 2)
    if taiex_closes:
        taiex_metrics["lastClose"] = taiex_closes[-1]

    # 2) 上市股票：直接抓 STOCK_DAY 近 3 個月
    stock_results = {}
    for code in TWSE_CODES:
        rows = fetch_stock_day_months(code, months=3)
        metrics = compute_metrics(rows)
        if metrics:
            stock_results[code] = metrics

    # 3) 上櫃股票：直接抓「個股日成交資訊」近 4 個月（含開高低收），跟上市股票一樣
    #    不再需要每日累積，同一次就能拿到完整月份歷史
    for code in OTC_CODES:
        rows = fetch_otc_stock_months(code, months=4)
        metrics = compute_metrics(rows)
        if metrics:
            stock_results[code] = metrics

    # 4) 個股相對大盤強弱（近5日、近20日漲跌幅相對加權指數的差）
    for code, m in stock_results.items():
        if "change5d" in m and "change5d" in taiex_metrics:
            m["relStrength5d"] = round(m["change5d"] - taiex_metrics["change5d"], 2)
        if "change20d" in m and "change20d" in taiex_metrics:
            m["relStrength20d"] = round(m["change20d"] - taiex_metrics["change20d"], 2)

    output = {
        "generated": today,
        "note": "MA10/MA20/20日60日高低點由官方歷史股價計算（上市：證交所STOCK_DAY，上櫃：櫃買中心個股日成交資訊）。相對強弱以加權指數(TAIEX)為比較基準，上櫃股票缺乏對應的櫃買指數精確資料，僅供方向性參考。",
        "taiex": taiex_metrics,
        "stocks": stock_results,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"寫入 {len(stock_results)} 檔股票技術指標，日期：{today}")


if __name__ == "__main__":
    main()
