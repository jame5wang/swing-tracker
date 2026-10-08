"""
抓取「追蹤股票」的公司重大訊息與個股新聞，寫入 data/company_news.json

為什麼要有這支腳本：
原本的新聞只抓 Yahoo股市「國際／台股」兩個 RSS 分類（每天一次、最多12則），幾乎都是大盤與美股
盤後新聞，沒有任何個股層級的消息，也沒有公開資訊觀測站的重大訊息——2026-10-07 奇鋐、富世達
「新莊廠遭調查局搜索」的重訊就完全沒被收錄。這支腳本補上三個個股層級的來源：

1. 公司重大訊息（官方）：證交所 OpenAPI /opendata/t187ap04_L（上市公司每日重大訊息）、
   櫃買中心 OpenAPI /mopsfin_t187ap04_O（上櫃公司每日重大訊息），只保留追蹤股票。
2. 鉅亨網台股新聞 API：每篇文章附有發布者標註的相關個股代號，可以精準對應到追蹤股票。
3. Google 新聞 RSS：逐檔以股票名稱搜尋近7天新聞，涵蓋 Yahoo、CMoney、經濟日報、工商時報等媒體。
   （只保存標題、來源、時間與連結，作為個人觀察筆記的新聞索引，不轉載內文。）

每個來源各自獨立：某個來源抓取失敗時，沿用上一次的資料，不會把已有的新聞清空。
重大訊息保留近30天、個股新聞保留近7天（每檔最多8則），滾動更新。
"""
import html
import json
import re
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

TZ = timezone(timedelta(hours=8))
OUTPUT_PATH = "data/company_news.json"
INDEX_HTML = "index.html"

TWSE_ANN_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
TPEX_ANN_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"
CNYES_URL = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=100&page={page}"
CNYES_PAGES = 3
GNEWS_URL = "https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
MOPS_LINK = "https://mops.twse.com.tw/mops/#/web/t05sr01_1"

ANN_KEEP_DAYS = 30
NEWS_KEEP_DAYS = 7
NEWS_PER_STOCK = 8
GNEWS_PAUSE = 1.2  # 每檔 Google 新聞查詢之間的間隔（秒），避免被限流

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# 股票名稱是常見詞、或容易搜到不相關新聞的，用全名搜尋 Google 新聞
QUERY_OVERRIDES = {
    "2455": "全新光電", "3167": "大量科技", "3443": "創意電子", "7750": "新代科技",
    "1303": "南亞塑膠", "6672": "騰輝電子", "6739": "竹陞科技", "4576": "大銀微系統",
    "3711": "日月光投控", "4971": "IET-KY", "7610": "聯友金屬", "8021": "尖點科技",
    "2049": "上銀科技", "6805": "富世達", "2464": "盟立自動化",
}


class FetchError(Exception):
    pass


def http_get(url, timeout=30, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429):  # 被限流就不要再重試，避免越打越久
                break
        except Exception as e:  # 讀取逾時（TimeoutError / IncompleteRead）也要接住
            last = e
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    raise FetchError(str(last))


def load_tracked():
    """追蹤清單以網站主清單 index.html 的 SEED_STOCKS 為準"""
    with open(INDEX_HTML, encoding="utf-8") as f:
        text = f.read()
    return dict(re.findall(r"code:'(\w+)',name:'([^']+)'", text))


def short_name(name):
    return re.sub(r"-(KY|創)$", "", name)


def roc_date(value):
    s = str(value or "").strip().replace("/", "")
    if not s.isdigit() or len(s) not in (7, 8):
        return None
    y, m, d = (int(s[:4]), int(s[4:6]), int(s[6:])) if len(s) == 8 else (int(s[:3]) + 1911, int(s[3:5]), int(s[5:]))
    try:
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def hhmm(value):
    s = str(value or "").strip()
    if not s.isdigit():
        return ""
    s = s.zfill(6)
    return f"{s[:2]}:{s[2:4]}"


def clean_text(value):
    return re.sub(r"[ \t]+", " ", str(value or "").replace("\r\n", "\n").replace("\r", "\n")).strip()


# ---------------------------------------------------------------- 1. 公司重大訊息
def fetch_announcements(tracked):
    out = []
    status = {}
    for market, url in (("上市", TWSE_ANN_URL), ("上櫃", TPEX_ANN_URL)):
        try:
            rows = json.loads(http_get(url).decode("utf-8-sig"))
            status[market] = f"ok（{len(rows)}筆）"
        except Exception as e:
            status[market] = f"失敗：{e}"
            print(f"::warning::{market}重大訊息抓取失敗：{e}")
            continue
        for raw in rows:
            row = {str(k).strip(): v for k, v in raw.items()}  # 證交所的「主旨 」欄位名稱帶有空白
            code = str(row.get("公司代號") or row.get("SecuritiesCompanyCode") or "").strip()
            if code not in tracked:
                continue
            date = roc_date(row.get("發言日期")) or roc_date(row.get("出表日期") or row.get("Date"))
            title = clean_text(row.get("主旨"))
            if not date or not title:
                continue
            item = {
                "code": code,
                "name": tracked[code],
                "market": market,
                "date": date,
                "time": hhmm(row.get("發言時間")),
                "title": title,
                "clause": clean_text(row.get("符合條款")),
                "factDate": roc_date(row.get("事實發生日")),
                "detail": clean_text(row.get("說明"))[:800],
                "source": "公開資訊觀測站",
                "url": MOPS_LINK,
            }
            item["id"] = f"{code}|{date}|{item['time']}|{title[:40]}"
            out.append(item)
    return out, status


# ---------------------------------------------------------------- 2. 鉅亨網（文章標註個股）
def fetch_cnyes(tracked):
    items = []
    for page in range(1, CNYES_PAGES + 1):
        try:
            data = json.loads(http_get(CNYES_URL.format(page=page)).decode("utf-8"))
        except Exception as e:
            if page == 1:
                raise
            print(f"鉅亨網第{page}頁抓取失敗：{e}")
            break
        for it in ((data.get("items") or {}).get("data") or []):
            codes = set()
            for m in it.get("market") or []:
                if isinstance(m, dict) and m.get("code"):
                    codes.add(str(m["code"]))
            for p in it.get("otherProduct") or []:
                parts = str(p).split(":")
                if len(parts) >= 2 and parts[0] == "TWS":
                    codes.add(parts[1])
            hit = sorted(c for c in codes if c in tracked)
            if not hit or not it.get("title") or not it.get("newsId"):
                continue
            ts = it.get("publishAt")
            published = datetime.fromtimestamp(ts, TZ).isoformat(timespec="minutes") if ts else None
            items.append({
                "codes": hit,
                "title": html.unescape(str(it["title"])).strip(),
                "source": "鉅亨網",
                "url": f"https://news.cnyes.com/news/id/{it['newsId']}",
                "publishedAt": published,
                "via": "cnyes",
            })
        time.sleep(0.5)
    return items


# ---------------------------------------------------------------- 3. Google 新聞（逐檔搜尋）
def fetch_google_news(code, name):
    short = short_name(name)
    query = QUERY_OVERRIDES.get(code) or short
    url = GNEWS_URL.format(q=urllib.parse.quote(f"{query} when:{NEWS_KEEP_DAYS}d"))
    root = ET.fromstring(http_get(url, retries=1))
    # 標題要真的提到這檔股票（簡稱、搜尋用全名或代號），排除只在內文順帶一提的新聞
    aliases = {short, query, code}
    out = []
    for item in root.findall(".//item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        if not any(a and a in title for a in aliases):
            continue
        try:
            published = parsedate_to_datetime(pub).astimezone(TZ).isoformat(timespec="minutes")
        except (TypeError, ValueError):
            published = None
        out.append({"codes": [code], "title": title, "source": source or "Google 新聞",
                    "url": link, "publishedAt": published, "via": "google"})
    return out


# ---------------------------------------------------------------- 標題點名多檔股票時一併標註
AMBIGUOUS_SHORT = {"2455", "3167", "3443", "7750", "1303"}  # 全新、大量、創意、新代、南亞：常見詞或會誤中他檔


def build_title_matchers(tracked):
    matchers = {}
    for code, name in tracked.items():
        alts = {QUERY_OVERRIDES.get(code)} if code in QUERY_OVERRIDES else set()
        if code not in AMBIGUOUS_SHORT:
            alts.add(short_name(name))
        alts = [re.escape(a) for a in alts if a]
        alts.append(rf"(?<!\d){code}(?!\d)")
        matchers[code] = re.compile("|".join(alts))
    return matchers


def tag_codes(items, matchers):
    for it in items:
        title = it.get("title") or ""
        extra = [c for c, rx in matchers.items() if c not in it["codes"] and rx.search(title)]
        if extra:
            it["codes"] = sorted(set(it["codes"]) | set(extra))
    return items


# ---------------------------------------------------------------- 合併與輸出
def norm_title(t):
    return re.sub(r"[\s\W_]+", "", t or "").lower()


def main():
    now = datetime.now(TZ)
    tracked = load_tracked()

    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            old = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        old = {}

    status = {}

    # 1) 重大訊息：新舊合併、依 id 去重、保留近30天
    anns, ann_status = fetch_announcements(tracked)
    status["announcements"] = ann_status
    merged = {a["id"]: a for a in old.get("announcements", [])}
    for a in anns:
        merged[a["id"]] = a
    cutoff_ann = (now - timedelta(days=ANN_KEEP_DAYS)).strftime("%Y-%m-%d")
    announcements = sorted(
        (a for a in merged.values() if a.get("date", "") >= cutoff_ann and a.get("code") in tracked),
        key=lambda a: (a["date"], a.get("time", "")), reverse=True,
    )

    # 2) + 3) 個股新聞
    fresh = []
    try:
        cn = fetch_cnyes(tracked)
        fresh.extend(cn)
        status["cnyes"] = f"ok（{len(cn)}則與追蹤股票相關）"
    except Exception as e:
        status["cnyes"] = f"失敗：{e}"
        print(f"::warning::鉅亨網新聞抓取失敗：{e}")

    g_ok = g_fail = 0
    g_blocked = False
    for code, name in tracked.items():
        if g_blocked:
            break
        try:
            fresh.extend(fetch_google_news(code, name))
            g_ok += 1
        except FetchError as e:
            g_fail += 1
            if "429" in str(e) or "403" in str(e):
                g_blocked = True
                print(f"::warning::Google 新聞被限流，這次先停止逐檔查詢：{e}")
        except ET.ParseError as e:
            g_fail += 1
            print(f"{code} Google 新聞解析失敗：{e}")
        time.sleep(GNEWS_PAUSE)
    status["google"] = f"成功 {g_ok} 檔、失敗 {g_fail} 檔" + ("（中途被限流）" if g_blocked else "")

    tag_codes(fresh, build_title_matchers(tracked))

    cutoff_news = (now - timedelta(days=NEWS_KEEP_DAYS)).isoformat(timespec="minutes")
    old_news = old.get("news") or {}
    news = {}
    for code in tracked:
        pool = list(old_news.get(code, []))
        for it in fresh:
            if code in it["codes"]:
                pool.append({k: v for k, v in it.items() if k != "codes"})
        seen = set()
        kept = []
        for it in sorted(pool, key=lambda x: x.get("publishedAt") or "", reverse=True):
            if (it.get("publishedAt") or "") < cutoff_news:
                continue
            key = norm_title(it.get("title"))[:40]
            if not key or key in seen or it.get("url") in seen:
                continue
            seen.add(key)
            seen.add(it.get("url"))
            kept.append(it)
            if len(kept) >= NEWS_PER_STOCK:
                break
        if kept:
            news[code] = kept

    output = {
        "generatedAt": now.isoformat(timespec="seconds"),
        "note": "公司重大訊息來自證交所／櫃買中心 OpenAPI（公開資訊觀測站每日重大訊息）；個股新聞來自鉅亨網台股新聞（文章標註個股）與 Google 新聞搜尋，僅保存標題與連結作為新聞索引。重大訊息保留近30天、個股新聞保留近7天。",
        "sources": status,
        "announcements": announcements,
        "news": news,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    print(f"重大訊息 {len(announcements)} 則（本次新抓 {len(anns)} 則）、個股新聞 {sum(len(v) for v in news.values())} 則（{len(news)} 檔有新聞）")
    print(json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    main()
