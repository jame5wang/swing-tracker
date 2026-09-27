"""
從公開資訊觀測站(MOPS)官方月營收公開資料，自動更新 data/research.json 裡每檔股票的
「單月營收」欄位。

資料來源：mopsfin.twse.com.tw/opendata/t187ap05_L.csv（上市）與
          mopsfin.twse.com.tw/opendata/t187ap05_O.csv（上櫃）
這是 MOPS 官方公開資料集（同一批資料也透過 openapi.twse.com.tw/v1/opendata/t187ap05_L
提供），每月公司申報完當月營收後更新，涵蓋全部上市櫃公司，不需要金鑰。

只更新「單月營收」這個欄位（monthly.period/value/yoy/mom），因為這是 MOPS 有提供
乾淨結構化資料的部分。季報EPS/毛利率營益率、法人報告、新聞摘要目前 MOPS 沒有對應的
乾淨開放資料集（財報要嘛是XBRL財報書、要嘛只能從「財務比較E點通」網頁畫面爬表格），
這部分仍由 Claude 定期研究彙整後更新（不是這支腳本的範圍）。
"""
import csv
import io
import json
import urllib.request
import urllib.error

RESEARCH_PATH = "data/research.json"
MOPS_URLS = {
    "TWSE": "https://mopsfin.twse.com.tw/opendata/t187ap05_L.csv",
    "OTC": "https://mopsfin.twse.com.tw/opendata/t187ap05_O.csv",
}

# 追蹤股票代號集合（跟 fetch_technical.py 的 TWSE_CODES/OTC_CODES 一致，僅用來過濾）
TRACKED_CODES = {
    "3037", "8046", "3189", "2383", "2368", "4958", "2313", "3711", "6239", "2303",
    "2344", "2408", "3481", "2308", "2301", "2455", "6213", "6672",
    "6274", "8358", "6182", "6488", "6147", "8299", "5289", "3105", "4971",
    "3017", "3653", "3167", "8021",  # 上市新增
    "3324", "3529", "6187", "6739",  # 上櫃新增
}


def http_get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    for enc in ("utf-8-sig", "utf-8", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_revenue_csv(text):
    """回傳 {code: {period, value, yoy, mom}}，取每個代號在檔案裡最新一筆（檔案通常只有當月）"""
    out = {}
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return out
    header = rows[0]

    def col(name):
        try:
            return header.index(name)
        except ValueError:
            return None

    i_period = col("資料年月")
    i_code = col("公司代號")
    i_cur = col("營業收入-當月營收")
    i_mom = col("營業收入-上月比較增減(%)")
    i_yoy = col("營業收入-去年同月增減(%)")
    if None in (i_period, i_code, i_cur):
        return out

    for row in rows[1:]:
        if len(row) <= max(i_period, i_code, i_cur):
            continue
        code = row[i_code].strip()
        if code not in TRACKED_CODES:
            continue
        try:
            period_raw = row[i_period].strip()  # 例："11508"
            year = int(period_raw[:-2]) + 1911
            month = int(period_raw[-2:])
            revenue_thousand = float(row[i_cur].replace(",", ""))
            revenue_billion = round(revenue_thousand / 100000, 2)  # 千元 -> 億元
        except (ValueError, IndexError):
            continue

        def fmt_pct(idx):
            if idx is None or len(row) <= idx or not row[idx].strip():
                return "—"
            try:
                v = float(row[idx])
                sign = "+" if v >= 0 else ""
                return f"{sign}{v:.2f}%"
            except ValueError:
                return "—"

        out[code] = {
            "period": f"{year}/{month:02d}",
            "value": f"{revenue_billion}億元",
            "yoy": fmt_pct(i_yoy),
            "mom": fmt_pct(i_mom),
        }
    return out


def main():
    revenue_by_code = {}
    any_success = False
    for market, url in MOPS_URLS.items():
        try:
            text = http_get_text(url)
        except (urllib.error.URLError, urllib.error.HTTPError):
            continue
        parsed = parse_revenue_csv(text)
        if parsed:
            any_success = True
            revenue_by_code.update(parsed)

    if not any_success:
        print("MOPS 月營收資料抓取失敗（兩個來源都無回應），不更動 research.json")
        return

    r = json.load(open(RESEARCH_PATH, encoding="utf-8"))
    updated = []
    for s in r["stocks"]:
        code = s["code"]
        new_monthly = revenue_by_code.get(code)
        if not new_monthly:
            continue
        research = s.setdefault("research", {})
        revenue = research.setdefault("revenue", {})
        old_period = (revenue.get("monthly") or {}).get("period")
        if old_period == new_monthly["period"]:
            continue  # 已經是最新月份，不用動
        revenue["monthly"] = new_monthly
        revenue.setdefault("sources", [])
        # 把 MOPS 標記為來源之一（如果還沒有的話），保留原本手動整理的季報/來源不動
        mops_src = {"title": f"公開資訊觀測站(MOPS) {s['code']} {new_monthly['period']} 月營收", "url": "https://mopsfin.twse.com.tw/opendata/"}
        if not any(src.get("title", "").startswith("公開資訊觀測站") for src in revenue["sources"]):
            revenue["sources"].insert(0, mops_src)
        updated.append(code)

    if updated:
        with open(RESEARCH_PATH, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
    print(f"MOPS 月營收更新完成，共更新 {len(updated)} 檔：{', '.join(updated) if updated else '（無需更新，皆為最新）'}")


if __name__ == "__main__":
    main()
