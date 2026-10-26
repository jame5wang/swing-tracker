# 測試分支用：列出指定股票的 Google 新聞原始結果與每一則被留下／排除的原因
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import fetch_company_news as m
from datetime import datetime, timedelta
codes = os.environ.get("DIAG_CODES", "").split(",")
tracked = m.load_tracked(); matchers = m.build_title_matchers(tracked); m.load_price_ranges()
cache = json.load(open(m.DATES_PATH, encoding="utf-8")).get("items", {}) if os.path.exists(m.DATES_PATH) else {}
now = datetime.now(m.TZ); cutoff = (now - timedelta(days=m.NEWS_KEEP_DAYS)).isoformat(timespec="minutes")
out = {}
for code in codes:
    q = m.stock_query(code, tracked[code])
    rows = []
    try:
        for it in m.google_search(q):
            aid = m.google_article_id(it["url"]); ent = cache.get(aid) or {}
            rows.append({"t": it["title"][:70], "src": it["source"], "site": it.get("site"), "g": it.get("publishedAt"),
                         "match": bool(matchers[code].search(it["title"])), "cat": m.news_category(it["title"]),
                         "real": ent.get("date"), "hint": m.stale_hint(it["title"], it.get("publishedAt") or "", [code])})
    except Exception as e:
        rows.append({"err": str(e)})
    out[code] = {"query": q, "rows": rows}
    import time; time.sleep(1.2)
json.dump(out, open("data/news_diag.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("diag done")
