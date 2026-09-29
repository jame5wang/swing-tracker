"""
用 data/technical.json 的逐日K資料校正 data/prices.json / data/prices_otc.json（只處理本站追蹤的股票）。

背景：2026-09-29 發現 TWSE 的 STOCK_DAY_ALL 批次彙總 API（data/prices.json 的來源）在盤後好幾個
小時內仍回傳上一個交易日（9/24）的收盤價，而且請求本身是成功的，重試機制抓不到這種「資料舊但沒報錯」
的情況；同一時間 fetch_technical.py 用的單股逐日K查詢已經有當天正確資料。

判斷原則（以「交易日」為準，而不是「收盤價是否相同」）：
1. 先找出 technical.json 目前最新的交易日 T（排除被標記 stale 的股票）。
2. 某檔股票的逐日K本身不是最新（最後一筆 < T，或被標記 stale）→ 不拿它去覆蓋任何資料，
   避免「技術指標抓取失敗、序列停在舊日期」反過來把正確的新股價蓋成舊的。
3. 收盤價資料列有交易日期（API 提供 Date 欄位）→ 只在該日期早於逐日K最新日期時才校正。
4. 沒有交易日期可比（API 沒提供 Date）→ 用「漲跌」推算的昨收比對：
   - 收盤價、昨收都跟逐日K最後兩筆一致 → 同一天，不動
   - 收盤價等於逐日K「前一天」收盤 → 批次資料落後一天，校正
   - 昨收等於逐日K「最後一天」收盤 → 批次資料反而比較新，不動
   - 其他無法判斷的情況 → 收盤價不同就校正（逐日K資料這次證實較即時），相同就只修漲跌
   之前只比收盤價：尖點(8021) 9/24、9/29 都收480，沒被校正，漲跌卻沿用9/24的+2，網站顯示錯誤的漲跌與昨收。
校正時一律用逐日K重算開高低收與漲跌，並把該列日期設成逐日K的日期。
"""
import json

TECHNICAL_PATH = "data/technical.json"
PRICE_FILES = ["data/prices.json", "data/prices_otc.json"]
EPS = 0.001


def to_float(v):
    try:
        return float(str(v).replace(",", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def same(a, b):
    return a is not None and b is not None and abs(a - b) < EPS


def decide(row, series):
    """回傳 (是否需要校正, 原因)"""
    last = series[-1]
    prev = series[-2] if len(series) >= 2 else None
    row_date = row.get("date")
    close = to_float(row.get("close"))
    change = to_float(row.get("change"))

    if row_date:
        if row_date < last["d"]:
            return True, f"收盤價資料日期 {row_date} 早於逐日K最新交易日 {last['d']}"
        return False, "收盤價資料日期不早於逐日K，不需校正"

    row_prev = (close - change) if (close is not None and change is not None) else None
    if same(close, last["c"]) and prev and same(row_prev, prev["c"]):
        return False, "收盤價與昨收皆與逐日K一致"
    if prev and same(close, prev["c"]) and not same(row_prev, last["c"]):
        return True, "收盤價等於逐日K前一交易日收盤，批次資料落後"
    if same(row_prev, last["c"]) and not same(close, last["c"]):
        return False, "批次資料比逐日K新一天，保留批次資料"
    if close is None or not same(close, last["c"]):
        return True, "收盤價與逐日K最新收盤不一致"
    return True, "收盤價一致但漲跌與逐日K不一致，修正漲跌"


def main():
    with open(TECHNICAL_PATH, encoding="utf-8") as f:
        technical = json.load(f)
    tech_stocks = technical.get("stocks") or {}

    current = [
        t["series"][-1]["d"]
        for t in tech_stocks.values()
        if t.get("series") and not t.get("stale")
    ]
    if not current:
        print("technical.json 沒有可用的最新逐日K資料，略過校正")
        return
    latest = max(current)

    total_patched = 0
    for path in PRICE_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        stocks = data.get("stocks") or []
        patched = []
        tracked_dates = []
        for row in stocks:
            code = str(row.get("code") or "").strip()
            t = tech_stocks.get(code)
            if not t or not t.get("series"):
                continue  # 非追蹤股票，不處理
            series = t["series"]
            last = series[-1]
            if t.get("stale") or last["d"] < latest:
                # 這檔的逐日K本身不是最新，不能拿來覆蓋收盤價
                if row.get("date"):
                    tracked_dates.append(row["date"])
                continue
            need, reason = decide(row, series)
            if need:
                prev = series[-2] if len(series) >= 2 else None
                row["close"] = last["c"]
                row["open"] = last.get("o")
                row["high"] = last.get("h")
                row["low"] = last.get("l")
                row["change"] = round(last["c"] - prev["c"], 4) if prev else None
                row["date"] = last["d"]
                row["reconciled"] = True
                patched.append(f"{code}（{reason}）")
            elif not row.get("date"):
                # 無日期但判定與逐日K同一天 → 補上日期，方便前端顯示每檔股價的交易日
                if same(to_float(row.get("close")), last["c"]):
                    row["date"] = last["d"]
            if row.get("date"):
                tracked_dates.append(row["date"])

        if tracked_dates:
            data["trackedDate"] = max(tracked_dates)
            # 檔案層級日期以追蹤股票實際的交易日為準（不再是腳本執行日）
            if data.get("dateSource") != "api" or data.get("date", "") < data["trackedDate"]:
                data["date"] = data["trackedDate"]
        data["reconciledFromTechnical"] = len(patched)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        if patched:
            print(f"{path}：用逐日K資料校正 {len(patched)} 檔：")
            for p in patched:
                print(f"  - {p}")
        else:
            print(f"{path}：追蹤股票資料與逐日K一致，不用校正")
        total_patched += len(patched)

    print(f"總計校正 {total_patched} 檔（逐日K最新交易日：{latest}）")


if __name__ == "__main__":
    main()
