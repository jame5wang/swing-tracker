"""
用 data/technical.json 的資料校正 data/prices.json / data/prices_otc.json。

背景：2026-09-29 發現 TWSE 的 STOCK_DAY_ALL 批次彙總 API（data/prices.json 的資料來源）
在當天盤後好幾個小時內都還沒更新到當天的收盤價，回傳的仍是上一個交易日（9/24）的舊資料，
但 fetch_technical.py 用的是「單一股票逐日K」查詢（STOCK_DAY，按月查詢），同一時間點
已經有當天的正確收盤價。兩個資料來源理論上該一致，但批次彙總API明顯有額外的發布延遲，
而且這種「API回傳成功、但資料本身是舊的」的狀況，fetch_prices.py 原本的重試機制
（處理的是連線失敗）完全抓不到，因為請求根本沒有失敗。

做法：以 technical.json 每檔股票 series 最後一筆（最新一個交易日）為準，
如果跟 prices.json / prices_otc.json 目前記錄的收盤價對不上，就用 technical.json 的
資料覆蓋過去，確保「現價」不會停留在舊的交易日。這只影響「本站有追蹤」的股票
（technical.json 只涵蓋這些股票），不會動到其他沒追蹤的股票資料。
"""
import json

TECHNICAL_PATH = "data/technical.json"
PRICE_FILES = ["data/prices.json", "data/prices_otc.json"]


def main():
    with open(TECHNICAL_PATH, encoding="utf-8") as f:
        technical = json.load(f)
    tech_stocks = technical.get("stocks") or {}

    total_patched = 0
    for path in PRICE_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        stocks = data.get("stocks") or []
        patched_here = 0
        latest_date = None
        for row in stocks:
            code = str(row.get("code") or "").strip()
            t = tech_stocks.get(code)
            if not t:
                continue
            series = t.get("series") or []
            if not series:
                continue
            last = series[-1]
            latest_close = last.get("c")
            if latest_close is None:
                continue
            try:
                cur_close = float(row.get("close"))
            except (TypeError, ValueError):
                cur_close = None
            if cur_close is not None and abs(cur_close - latest_close) < 0.01:
                continue  # 已經一致，不用動
            prev_close = None
            if len(series) >= 2:
                prev_close = series[-2].get("c")
            row["close"] = latest_close
            row["open"] = last.get("o")
            row["high"] = last.get("h")
            row["low"] = last.get("l")
            if prev_close:
                row["change"] = round(latest_close - prev_close, 4)
            patched_here += 1
            latest_date = last.get("d")
        if patched_here:
            if latest_date:
                data["date"] = latest_date
            data["reconciledFromTechnical"] = patched_here
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            print(f"{path}：用技術指標資料校正了 {patched_here} 檔股票的收盤價")
            total_patched += patched_here
        else:
            print(f"{path}：資料已與技術指標一致，不用校正")

    print(f"總計校正 {total_patched} 檔")


if __name__ == "__main__":
    main()
