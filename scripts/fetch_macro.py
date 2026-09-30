"""
抓取美股/國際指標，計算簡易風險燈號，寫入 data/macro.json
資料來源：Yahoo Finance 公開圖表 API（query1.finance.yahoo.com/v8/finance/chart/{symbol}），
免費、無需金鑰，被 yfinance 等主流開源套件廣泛採用；未提供官方 SLA，若抓取失敗會保留
上次成功結果並標記 stale，不會讓整份報告消失。

風險燈號是簡化的規則式框架，僅供「觀察國際情緒」參考，不是資產配置建議：
最終要不要調整持股水位，仍需自行判斷。
"""
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=10d"
OUTPUT_PATH = "data/macro.json"

SYMBOLS = {
    "dji": "^DJI",
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "sox": "^SOX",
    "vix": "^VIX",
    "dxy": "DX-Y.NYB",
    "tsmAdr": "TSM",
}

# 已知的 2026 年重要財經事件日期（美東時間日期），公開行事曆固定排程，非即時抓取。
# - FOMC 利率決議：federalreserve.gov 官方會議日程（美東 14:00 公布聲明）
# - CPI／非農／PPI：美國勞工統計局(BLS)官方2026年排程（美東 8:30 公布）
# - PCE／GDP：美國經濟分析局(BEA)官方發布排程 bea.gov/news/schedule（美東 8:30 公布）
#   PCE 是聯準會最重視的通膨指標，2026-09-30 使用者指出行事曆漏掉 PCE 後補上。
# 2026-09-29/30 已逐一核對官方排程。前端會依美東時間換算成台北時間顯示（含美國夏令時間切換）。
# 對台股的影響通常反映在「下一個台股交易日」。
US_DATA_TIME = "08:30"   # BLS/BEA 數據公布時間（美東）
FOMC_TIME = "14:00"      # FOMC 聲明公布時間（美東）

KNOWN_EVENTS_2026 = [
    ("2026-01-28", "FOMC利率決議"),
    ("2026-03-18", "FOMC利率決議"),
    ("2026-04-29", "FOMC利率決議"),
    ("2026-06-17", "FOMC利率決議"),
    ("2026-07-29", "FOMC利率決議"),
    ("2026-09-16", "FOMC利率決議"),
    ("2026-10-28", "FOMC利率決議"),
    ("2026-12-09", "FOMC利率決議"),
    ("2026-01-13", "美國12月CPI"), ("2026-02-13", "美國1月CPI"), ("2026-03-11", "美國2月CPI"),
    ("2026-04-10", "美國3月CPI"), ("2026-05-12", "美國4月CPI"), ("2026-06-10", "美國5月CPI"),
    ("2026-07-14", "美國6月CPI"), ("2026-08-12", "美國7月CPI"), ("2026-09-11", "美國8月CPI"),
    ("2026-10-14", "美國9月CPI"), ("2026-11-10", "美國10月CPI"), ("2026-12-10", "美國11月CPI"),
    ("2026-01-09", "美國非農就業"), ("2026-02-11", "美國非農就業"), ("2026-03-06", "美國非農就業"),
    ("2026-04-03", "美國非農就業"), ("2026-05-08", "美國非農就業"), ("2026-06-05", "美國非農就業"),
    ("2026-07-02", "美國非農就業"), ("2026-08-07", "美國非農就業"), ("2026-09-04", "美國非農就業"),
    ("2026-10-02", "美國非農就業"), ("2026-11-06", "美國非農就業"), ("2026-12-04", "美國非農就業"),
    # PCE 物價指數（BEA Personal Income and Outlays）
    ("2026-09-30", "美國8月PCE物價指數"), ("2026-10-29", "美國9月PCE物價指數"),
    ("2026-11-25", "美國10月PCE物價指數"), ("2026-12-23", "美國11月PCE物價指數"),
    # GDP（BEA，與 PCE 同日公布）
    ("2026-10-29", "美國第3季GDP初值"), ("2026-11-25", "美國第3季GDP修正值"), ("2026-12-23", "美國第3季GDP終值"),
    # PPI 生產者物價指數（BLS）
    ("2026-10-15", "美國9月PPI"), ("2026-11-13", "美國10月PPI"), ("2026-12-15", "美國11月PPI"),
]


def event_time_et(name):
    """回傳這個事件的美東公布時間；財報類事件（台積電法說、美國科技股財報）沒有固定時間回傳 None"""
    if name.startswith("FOMC"):
        return FOMC_TIME
    if name.startswith("美國"):
        return US_DATA_TIME
    return None


def taipei_time_label(date_str, et_time):
    """美東日期+時間 → 台北時間標籤，例如「台北 20:30」或「台北 隔日02:00」；自動處理美國夏令時間。"""
    if not et_time:
        return None
    try:
        from zoneinfo import ZoneInfo
        et = datetime.strptime(f"{date_str} {et_time}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/New_York"))
        tpe = et.astimezone(ZoneInfo("Asia/Taipei"))
    except Exception:
        return None
    prefix = "隔日" if tpe.date().isoformat() != date_str else ""
    return f"台北 {prefix}{tpe.strftime('%H:%M')}"

# AI資本支出／超大型雲端業者財報行事曆：這些公司的財報與資本支出指引，常是牽動
# 半導體供應鏈（含本站追蹤標的）股價的關鍵日，故獨立標記為「高波動關注日」。
# 日期來源：公司官方投資人關係頁面（已公告者）或主流財經資訊平台的預估排程（未正式確認者，
# 名稱會標註「預估」），會隨公司後續正式公告調整。
AI_CAPEX_EARNINGS_2026 = [
    ("2026-10-15", "台積電 3Q26法說會"),
    ("2026-10-28", "微軟 FY27 Q1財報（預估，未正式確認）"),
    ("2026-10-28", "Alphabet(Google) 3Q26財報（預估，未正式確認）"),
    ("2026-10-28", "Meta 3Q26財報（預估，未正式確認）"),
    ("2026-11-17", "NVIDIA 3Q FY27財報（預估，未正式公告）"),
]


def http_get_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch_symbol(symbol):
    try:
        raw = http_get_json(CHART_URL.format(symbol=symbol))
        result = raw["chart"]["result"][0]
        meta = result["meta"]
        closes = result["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) < 2:
            return None
        last, prev = closes[-1], closes[-2]
        change_pct = round((last - prev) / prev * 100, 2)
        out = {
            "price": round(last, 2),
            "changePct": change_pct,
            "regularMarketTime": meta.get("regularMarketTime"),
        }
        # 5日累計漲跌：費半(SOX)常領先台股約1~2週反應（如2026年7月AI資本支出疑慮事件），
        # 單日漲跌容易忽略連續轉弱的趨勢，額外算5日變化供領先指標判讀用。
        if len(closes) >= 6 and closes[-6]:
            out["change5dPct"] = round((last - closes[-6]) / closes[-6] * 100, 2)
        return out
    except Exception as e:  # 包含讀取逾時 TimeoutError / IncompleteRead，之前沒接住會讓整支腳本當掉
        print(f"{symbol} 抓取失敗：{e}")
        return None


def compute_risk_light(data):
    """簡易規則式風險燈號：非投資建議，只是把幾個常見的市場情緒指標轉成燈號。"""
    score = 0
    reasons = []

    vix = data.get("vix")
    if vix:
        if vix["price"] >= 28:
            score += 2
            reasons.append(f"VIX達{vix['price']}（恐慌情緒偏高）")
        elif vix["price"] >= 20:
            score += 1
            reasons.append(f"VIX為{vix['price']}（波動略偏高）")

    sox = data.get("sox")
    if sox:
        if sox["changePct"] <= -3:
            score += 2
            reasons.append(f"費半重挫{sox['changePct']}%")
        elif sox["changePct"] <= -1:
            score += 1
            reasons.append(f"費半下跌{sox['changePct']}%")
        elif sox["changePct"] >= 2:
            reasons.append(f"費半上漲{sox['changePct']}%（偏正面）")
        c5 = sox.get("change5dPct")
        if c5 is not None:
            if c5 <= -5:
                score += 1
                reasons.append(f"費半近5日累計跌{c5}%（領先指標轉弱，留意擴散至台股半導體供應鏈）")
            elif c5 >= 5:
                reasons.append(f"費半近5日累計漲{c5}%（領先指標偏正面）")

    tsm = data.get("tsmAdr")
    if tsm:
        if tsm["changePct"] <= -3:
            score += 1
            reasons.append(f"台積電ADR下跌{tsm['changePct']}%")

    us_indices = [data.get(k) for k in ("dji", "sp500", "nasdaq") if data.get(k)]
    if us_indices:
        avg = sum(i["changePct"] for i in us_indices) / len(us_indices)
        if avg <= -1.5:
            score += 2
            reasons.append(f"美股三大指數平均跌{avg:.1f}%")
        elif avg <= -0.5:
            score += 1
            reasons.append(f"美股三大指數平均跌{avg:.1f}%")
        elif avg >= 1:
            reasons.append(f"美股三大指數平均漲{avg:.1f}%（偏正面）")

    if score >= 4:
        level = "red"
        label = "風險偏高"
    elif score >= 2:
        level = "yellow"
        label = "留意觀察"
    else:
        level = "green"
        label = "風險偏低"

    if not reasons:
        reasons.append("主要指標無明顯異常")
    stale_names = {"vix": "VIX", "sox": "費半", "tsmAdr": "台積電ADR", "dji": "道瓊", "sp500": "S&P500", "nasdaq": "那斯達克"}
    stale_list = [stale_names[k] for k in stale_names if (data.get(k) or {}).get("stale")]
    if stale_list:
        reasons.append(f"（{'、'.join(stale_list)}本次抓取失敗，沿用前次數值計算）")

    return {"level": level, "label": label, "score": score, "reasons": reasons}


def upcoming_events(today_str, days=5):
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    out = []
    all_events = [(d, n, False) for d, n in KNOWN_EVENTS_2026] + \
                 [(d, n, True) for d, n in AI_CAPEX_EARNINGS_2026]
    for date_str, name, watch in all_events:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        delta = (d - today).days
        if 0 <= delta <= days:
            out.append({
                "date": date_str, "name": name, "daysAway": delta, "watch": watch,
                "tpeTime": taipei_time_label(date_str, event_time_et(name)),
            })
    out.sort(key=lambda e: (e["date"], e["name"]))
    return out


def main():
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).strftime("%Y-%m-%d")

    data = {}
    for key, symbol in SYMBOLS.items():
        result = fetch_symbol(symbol)
        if result:
            data[key] = result

    # 若這次抓取整體失敗（例如來源暫時擋掉），盡量保留舊資料而非清空整份報告
    old = {}
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    stale = len(data) == 0
    stale_symbols = []
    if stale and old.get("indices"):
        indices = old["indices"]
        generated = old.get("generated", today)
    else:
        indices = dict(data)
        generated = today
        # 部分指標抓取失敗：沿用上次數值並逐項標記 stale。之前是直接把失敗的指標丟掉，
        # 例如費半抓不到時風險燈號會少算費半的分數，黃燈可能被誤判成綠燈。
        for key in SYMBOLS:
            if key not in indices and (old.get("indices") or {}).get(key):
                prev = dict(old["indices"][key])
                prev["stale"] = True
                indices[key] = prev
                stale_symbols.append(key)

    risk_light = compute_risk_light(indices) if indices else {
        "level": "gray", "label": "資料暫缺", "score": 0, "reasons": ["國際指標資料暫時無法取得"]
    }

    output = {
        "generated": generated,
        "stale": stale,
        "staleSymbols": stale_symbols,
        "indices": indices,
        "riskLight": risk_light,
        "upcomingEvents": upcoming_events(today, days=10),
        "note": "指標來源 Yahoo Finance 公開資料，風險燈號為簡化規則式框架（VIX／費半單日與5日變化／台積電ADR／美股三大指數綜合評分）；財經事件依聯準會、BLS、BEA 官方排程（FOMC／CPI／非農／PCE／GDP／PPI），並含台積電/NVIDIA/微軟/Google/Meta財報等AI資本支出高波動關注日，僅供觀察國際市場情緒參考，不構成投資或資產配置建議。",
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"寫入盤前簡報，日期：{generated}，抓到 {len(data)}/{len(SYMBOLS)} 項指標，stale={stale}")


if __name__ == "__main__":
    main()
