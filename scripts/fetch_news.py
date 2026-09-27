"""
抓取財經新聞頭條（國際總經 + 台股），寫入 data/news.json。
資料來源：Yahoo股市官方 RSS 服務（https://tw.stock.yahoo.com/rss-index），
公開、免費、無需金鑰，非爬蟲行為。

目的：盤前總經簡報目前只有量化指標（指數漲跌、風險燈號），缺少「消息面」——
例如川普關稅發言、AI巨擘動態、產業黑天鵝事件、市場恐慌情緒報導等，這些常常是
隔天台股開盤前最需要留意的東西。這支腳本只做「彙整」，不做真假查證或投資判斷，
挑選哪些新聞要看、怎麼解讀，仍需自行判斷。
"""
import json
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

OUTPUT_PATH = "data/news.json"
MAX_ITEMS = 10

FEEDS = [
    {"url": "https://tw.stock.yahoo.com/rss?category=intl-markets", "tag": "國際"},
    {"url": "https://tw.stock.yahoo.com/rss?category=tw-market", "tag": "台股"},
]


def http_get(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def fetch_feed(feed):
    try:
        raw = http_get(feed["url"])
        root = ET.fromstring(raw)
        items = []
        for item in root.findall(".//item"):
            title = strip_html((item.findtext("title") or "").strip())
            link = (item.findtext("link") or "").strip()
            pub_date_raw = (item.findtext("pubDate") or "").strip()
            if not title or not link:
                continue
            try:
                dt = parsedate_to_datetime(pub_date_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            items.append({
                "title": title,
                "link": link,
                "source": feed["tag"],
                "pubDate": dt.astimezone(timezone.utc).isoformat(),
            })
        return items
    except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, ValueError):
        return None


def main():
    all_items = []
    any_success = False
    for feed in FEEDS:
        items = fetch_feed(feed)
        if items is not None:
            any_success = True
            all_items.extend(items)

    now = datetime.now(timezone.utc)

    if not any_success:
        # 全部來源都失敗：保留舊資料，只標記 stale，不讓整份簡報消失
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing["stale"] = True
            with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        except (FileNotFoundError, json.JSONDecodeError):
            with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                json.dump({"generated": now.strftime("%Y-%m-%d"), "stale": True, "items": []}, f, ensure_ascii=False, indent=2)
        return

    # 依標題去重（同一則新聞常被多分類重複收錄），再依時間新到舊排序
    seen_titles = set()
    deduped = []
    all_items.sort(key=lambda x: x["pubDate"], reverse=True)
    for it in all_items:
        key = it["title"]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        deduped.append(it)

    out = {
        "generated": now.strftime("%Y-%m-%d"),
        "generatedAt": now.isoformat(),
        "stale": False,
        "items": deduped[:MAX_ITEMS],
        "note": "來源：Yahoo股市 RSS（國際財經／台股）自動彙整最新標題，非人工編選，僅供參考，不構成投資建議。",
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
