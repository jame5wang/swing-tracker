import json, time, urllib.request, urllib.parse
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "zh-TW,zh;q=0.9"}
def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()
def shape(o, depth=0):
    if isinstance(o, dict):
        return {k: shape(v, depth + 1) for k, v in list(o.items())[:30]}
    if isinstance(o, list):
        if not o: return []
        return [shape(o[0], depth + 1), f"...len={len(o)}"] + ([shape(o[-1], depth + 1)] if len(o) > 1 else [])
    return o if not isinstance(o, str) or len(o) < 300 else o[:300]
urls = {
 "twse_rwd_MS_1027": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=20261027&selectType=MS&response=json",
 "twse_rwd_MS_1022": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=20261022&selectType=MS&response=json",
 "twse_rwd_MS_1023": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=20261023&selectType=MS&response=json",
 "twse_old_MS_1022": "https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date=20261022&selectType=MS",
 "twse_openapi": "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN",
 "tpex_new_1027": "https://www.tpex.org.tw/www/zh-tw/margin/balance?date=2026%2F10%2F27&id=&response=json",
 "tpex_new_1022": "https://www.tpex.org.tw/www/zh-tw/margin/balance?date=2026%2F10%2F22&id=&response=json",
 "tpex_old_1022": "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&o=json&d=115/10/22",
 "tpex_openapi": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance",
 "tpex_sum_new": "https://www.tpex.org.tw/www/zh-tw/margin/sum?date=2026%2F10%2F22&response=json",
}
out = {}
for k, u in urls.items():
    try:
        st, raw = get(u)
        txt = raw.decode("utf-8-sig", "replace")
        try:
            j = json.loads(txt)
            out[k] = {"status": st, "bytes": len(raw), "shape": shape(j)}
            if isinstance(j, dict):
                # keep full small tables & any total rows
                for kk, vv in j.items():
                    if isinstance(vv, list) and vv and isinstance(vv[0], dict) and "data" in vv[0]:
                        out[k]["tables_full"] = [{"title": t.get("title"), "fields": t.get("fields"), "date": t.get("date"),
                                                   "rows": (t.get("data") or [])[:3], "nrows": len(t.get("data") or []),
                                                   "summary": t.get("summary"), "notes": t.get("notes"), "totalCount": t.get("totalCount")} for t in vv]
                for kk in ("tfootData_one", "tfootData_two", "creditList", "creditFields", "reportDate", "date", "stat", "title"):
                    if kk in j: out[k][kk] = j[kk]
        except ValueError:
            out[k] = {"status": st, "bytes": len(raw), "head": txt[:600]}
    except Exception as e:
        out[k] = {"err": str(e)}
    time.sleep(1.5)
json.dump(out, open("probe/margin_report.json", "w"), ensure_ascii=False, indent=1)
print("done")
