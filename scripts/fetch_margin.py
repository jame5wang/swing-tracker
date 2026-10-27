"""
抓取上市（證交所）與上櫃（櫃買中心）的大盤融資融券餘額，寫入 data/margin.json，保留近 70 個交易日
（網站顯示近 60 日）。使用者 2026-10-27 要求：「增加櫃買／上市融資融券餘額，每日更新」，同日改成近60日。

資料來源（官方公開資料）：
- 上市：證交所「信用交易統計」
  https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=YYYYMMDD&selectType=MS&response=json
  取「融資(交易單位)」「融券(交易單位)」「融資金額(仟元)」三列的前日餘額／今日餘額。
- 上櫃：櫃買中心「上櫃股票融資融券餘額」
  https://www.tpex.org.tw/www/zh-tw/margin/balance?date=YYYY/MM/DD&id=&response=json
  取 summary 的「合計(張)」「融資金(仟元)」兩列（前資餘額／資餘額、前券餘額／券餘額）。

公布時間：兩家都是盤後晚上約 20:30～22:00 才公布當天資料，所以 18:00 那次會抓到前一個交易日，
21:50 那次補抓當天（見 .github/workflows/fetch-margin.yml）。

證交所註明「授信機構隔天還會調帳，請以『前日餘額』為準，『今日餘額』為輔助參考」：
最新一天先用今日餘額（標記 prov＝初值），抓到下一個交易日時，改用那天報表上的前日餘額更正。
休市日兩邊都回傳「沒有符合條件的資料」，直接略過。
"""
import json
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
OUTPUT_PATH = "data/margin.json"
KEEP_DAYS = 70          # 保留的交易日數（網站顯示 60 日，多留幾天可以算 60 日變化）
BACKFILL_DAYS = 62      # 資料少於這個交易日數時往回補
BACKFILL_LOOKBACK = 100  # 往回補最多回溯幾個日曆日（60 個交易日約 85～90 個日曆日）
RECENT_LOOKBACK = 7     # 平常每次重抓最近幾個日曆日（補漏抓的日子、更正前一日餘額）
PAUSE = 2.0             # 證交所對短時間大量請求會暫時封鎖，請求之間留間隔

TWSE_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ymd}&selectType=MS&response=json"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/margin/balance?date={y}%2F{m:02d}%2F{d:02d}&id=&response=json"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def num(value):
    s = str(value if value is not None else "").replace(",", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return None


def get_json(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8-sig"))
        except Exception as e:  # 逾時、連線中斷、被暫時封鎖都重試
            last = e
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    raise last


def col(fields, name, default):
    try:
        return fields.index(name)
    except (ValueError, AttributeError):
        return default


def fetch_twse(day):
    """回傳 {finAmt, finLots, shortLots, finAmtPrev, finLotsPrev, shortLotsPrev}；休市日回傳 None。金額單位：仟元，張數：張。"""
    j = get_json(TWSE_URL.format(ymd=day.strftime("%Y%m%d")))
    if str(j.get("stat", "")).upper() != "OK" or j.get("date") not in (None, day.strftime("%Y%m%d")):
        return None
    for t in j.get("tables") or []:
        rows = {str(r[0]).strip(): r for r in (t.get("data") or []) if r}
        fin, short, amt = rows.get("融資(交易單位)"), rows.get("融券(交易單位)"), rows.get("融資金額(仟元)")
        if not (fin and short and amt):
            continue
        fields = t.get("fields") or []
        i_prev, i_today = col(fields, "前日餘額", 4), col(fields, "今日餘額", 5)
        rec = {
            "finAmt": num(amt[i_today]), "finLots": num(fin[i_today]), "shortLots": num(short[i_today]),
            "finAmtPrev": num(amt[i_prev]), "finLotsPrev": num(fin[i_prev]), "shortLotsPrev": num(short[i_prev]),
        }
        return rec if rec["finAmt"] and rec["finLots"] else None
    return None


def fetch_tpex(day):
    j = get_json(TPEX_URL.format(y=day.year, m=day.month, d=day.day))
    if str(j.get("stat", "")).lower() != "ok":
        return None
    roc = f"{day.year - 1911}/{day.month:02d}/{day.day:02d}"
    for t in j.get("tables") or []:
        if str(t.get("date", "")).strip() != roc:
            continue  # 休市日可能回傳別天的資料，日期對不上就不收
        summary = {str(r[1]).strip(): r for r in (t.get("summary") or []) if len(r) > 2}
        tot, amt = summary.get("合計(張)"), summary.get("融資金(仟元)")
        if not (tot and amt):
            continue
        fields = t.get("fields") or []
        f_prev, f_bal = col(fields, "前資餘額(張)", 2), col(fields, "資餘額", 6)
        s_prev, s_bal = col(fields, "前券餘額(張)", 10), col(fields, "券餘額", 14)
        rec = {
            "finAmt": num(amt[f_bal]), "finLots": num(tot[f_bal]), "shortLots": num(tot[s_bal]),
            "finAmtPrev": num(amt[f_prev]), "finLotsPrev": num(tot[f_prev]), "shortLotsPrev": num(tot[s_prev]),
        }
        return rec if rec["finAmt"] and rec["finLots"] else None
    return None


def main():
    now = datetime.now(TZ)
    today = now.date()
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            old = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        old = {}

    markets = {"twse": ("上市", fetch_twse), "tpex": ("上櫃", fetch_tpex)}
    series = {k: {r["date"]: r for r in (old.get(k) or []) if r.get("date")} for k in markets}
    have = min(len(v) for v in series.values())
    if have < BACKFILL_DAYS:
        start = today - timedelta(days=BACKFILL_LOOKBACK)
    else:
        latest = max(max(v) for v in series.values())
        start = min(date.fromisoformat(latest), today) - timedelta(days=RECENT_LOOKBACK)
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]  # 週末不開盤

    status = {k: {"ok": 0, "closed": 0, "failed": []} for k in markets}
    failed_days = {k: set() for k in markets}
    for d in days:
        ds = d.isoformat()
        for key, (label, fetch) in markets.items():
            try:
                rec = fetch(d)
            except Exception as e:
                status[key]["failed"].append(f"{ds}（{str(e)[:40]}）")
                failed_days[key].add(ds)
                print(f"::warning::{label} {ds} 融資融券抓取失敗：{e}")
                time.sleep(PAUSE)
                continue
            time.sleep(PAUSE)
            if not rec:
                status[key]["closed"] += 1
                continue
            status[key]["ok"] += 1
            s = series[key]
            # 前一個交易日改用這天報表上的「前日餘額」（授信機構隔日調帳後的正確數字）；
            # 中間有抓取失敗的日子就不更正，避免把別天的數字寫錯位置
            earlier = sorted(k for k in s if k < ds)
            if earlier and not any(earlier[-1] < f < ds for f in failed_days[key]):
                prev = s[earlier[-1]]
                if rec["finAmtPrev"] and rec["finLotsPrev"]:
                    prev.update(finAmt=rec["finAmtPrev"], finLots=rec["finLotsPrev"], shortLots=rec["shortLotsPrev"])
                    prev.pop("prov", None)
            if ds in s and not s[ds].get("prov"):
                continue  # 已經用下一個交易日的前日餘額更正過，不要再蓋回今日餘額初值
            s[ds] = {"date": ds, "finAmt": rec["finAmt"], "finLots": rec["finLots"], "shortLots": rec["shortLots"], "prov": True}

    out = {
        "generated": now.isoformat(timespec="minutes"),
        "note": "上市資料來自證交所「信用交易統計」、上櫃資料來自櫃買中心「上櫃股票融資融券餘額」合計列。finAmt＝融資餘額（仟元），finLots＝融資餘額（張），shortLots＝融券餘額（張）。prov＝當日初值：授信機構隔日仍會調帳，下一個交易日公布後改以其「前日餘額」更正。兩家約在盤後晚上 20:30～22:00 公布當日資料。",
        "status": {k: {"成功天數": v["ok"], "休市或尚未公布": v["closed"], "失敗": v["failed"]} for k, v in status.items()},
    }
    for key in markets:
        rows = sorted(series[key].values(), key=lambda r: r["date"])[-KEEP_DAYS:]
        out[key] = rows
    dates = [out[k][-1]["date"] for k in markets if out[k]]
    out["latestDate"] = min(dates) if len(dates) == len(markets) else (dates[0] if dates else None)

    if not any(out[k] for k in markets):
        print("::warning::兩個市場都沒有取得任何融資融券資料，保留原檔案不覆寫")
        return
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    for key, (label, _) in markets.items():
        last = out[key][-1] if out[key] else None
        if last:
            print(f"{label} {last['date']}：融資 {last['finAmt'] / 1e5:,.1f} 億元（{last['finLots']:,} 張）、融券 {last['shortLots']:,} 張{'（初值）' if last.get('prov') else ''}")
    print(json.dumps(out["status"], ensure_ascii=False))


if __name__ == "__main__":
    main()
