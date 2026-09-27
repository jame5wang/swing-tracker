"""
抓取財經新聞頭條（國際總經 + 台股），標記與你持股相關的個股，並依日期歸檔到
data/news/YYYY-MM-DD.json（索引在 data/news/index.json），讓網站可以回顧過去
幾天的焦點消息，不會每天覆蓋掉舊資料。

資料來源：Yahoo股市官方 RSS 服務（https://tw.stock.yahoo.com/rss-index），
公開、免費、無需金鑰，非爬蟲行為。

「相關持股」比對邏輯：
1. 直接比對新聞標題是否出現你追蹤的27檔個股名稱／代號。
2. 主題關鍵字比對——例如新聞提到「輝達」「GB300」「AI伺服器」等AI晶片供應鏈
   關鍵字時，即使沒有直接點名某檔台股，也會關聯到你持股中屬於該供應鏈的個股
   （ABF載板／CCL／封測／矽晶圓等）。這是規則式關鍵字比對，不是語意理解，
   只能抓到明顯的產業關聯，仍需自行判斷新聞對個股的實際影響。

這支腳本只做「彙整＋關聯標記」，不做真假查證或投資判斷。
"""
import json
import os
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

try:
    from zoneinfo import ZoneInfo
    TAIPEI = ZoneInfo("Asia/Taipei")
except ImportError:
    TAIPEI = timezone(timedelta(hours=8))

OUTPUT_DIR = "data/news"
INDEX_PATH = "data/news/index.json"
MAX_ITEMS = 12
MAX_ARCHIVE_DATES = 60

FEEDS = [
    {"url": "https://tw.stock.yahoo.com/rss?category=intl-markets", "tag": "國際"},
    {"url": "https://tw.stock.yahoo.com/rss?category=tw-market", "tag": "台股"},
]

# 追蹤中的27檔個股：代號 -> 名稱（用於直接比對新聞標題是否點名）
STOCK_NAMES = {
    "3037": "欣興", "8046": "南電", "3189": "景碩",
    "2383": "台光電", "6274": "台燿", "6213": "聯茂", "6672": "騰輝電子",
    "2368": "金像電", "4958": "臻鼎", "8358": "金居", "2313": "華通",
    "6182": "合晶", "6488": "環球晶",
    "3711": "日月光", "6239": "力成", "6147": "頎邦",
    "2303": "聯電",
    "2344": "華邦電", "2408": "南亞科", "8299": "群聯", "5289": "宜鼎",
    "3481": "群創",
    "2308": "台達電", "2301": "光寶科",
    "2455": "全新", "3105": "穩懋", "4971": "IET-KY",
    "3017": "奇鋐", "3653": "健策", "3324": "雙鴻",
    "3529": "力旺", "6187": "萬潤", "6739": "竹陞科技",
    "3167": "大量", "8021": "尖點",
}

# 主題關鍵字 -> 受影響的持股代號（供應鏈/產業關聯，不需要新聞直接點名個股）
THEME_KEYWORDS = {
    # AI晶片／伺服器／先進封裝供應鏈：載板、CCL、封測、矽晶圓、PCB 都跟這波景氣連動
    r"輝達|NVIDIA|GB300|GB200|Rubin|CoWoS|HBM|AI伺服器|AI晶片|AI算力|資料中心": [
        "3037", "8046", "3189",  # ABF載板
        "2383", "6274", "6213", "6672",  # CCL
        "2368", "4958", "8358", "2313",  # PCB
        "3711", "6239", "6147",  # 封測
        "6182", "6488",  # 矽晶圓
        "3017", "3653", "3324",  # AI伺服器散熱(均熱板/液冷)
        "3167",  # 先進封裝研磨拋光設備
        "8021",  # 高階基板PCB鑽針
    ],
    # 記憶體漲價／缺貨／三星SK海力士美光動態
    r"記憶體|DRAM|NAND|三星|SK海力士|美光|Micron|memory": [
        "2344", "2408", "8299", "5289",
    ],
    # 聯電／成熟製程代工（注意：不要用「台積電」「先進製程」等關鍵字，
    # 那些新聞絕大多數是台積電自己的擴產/財報消息，跟聯電的成熟製程業務關聯很弱，
    # 掛上去只會製造雜訊）
    r"聯電|UMC|成熟製程|車用晶片代工": [
        "2303",
    ],
    # 化合物半導體／砷化鎵／衛星通訊／電動車功率元件
    r"砷化鎵|GaN|氮化鎵|PA|功率元件|衛星通訊|矽光子": [
        "2455", "3105", "4971",
    ],
    # 面板／車用顯示
    r"面板|MiniLED|車用顯示": [
        "3481",
    ],
    # 電源、散熱、伺服器機殼
    r"電源供應器|散熱|伺服器機殼|台達電|光寶科": [
        "2308", "2301",
    ],
}


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
    return re.sub(r"<[^>]+>", "", text).strip()


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


def related_stocks_for(title):
    """回傳這則新聞標題關聯到的持股清單 [{code,name}]，依直接點名優先，其次主題比對。"""
    matched = {}
    for code, name in STOCK_NAMES.items():
        if name in title:
            matched[code] = name
    for pattern, codes in THEME_KEYWORDS.items():
        if re.search(pattern, title, re.IGNORECASE):
            for code in codes:
                if code not in matched:
                    matched[code] = STOCK_NAMES.get(code, code)
    return [{"code": c, "name": n} for c, n in matched.items()]


def load_index():
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("dates", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_index(dates):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    dates = sorted(set(dates), reverse=True)[:MAX_ARCHIVE_DATES]
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump({"dates": dates}, f, ensure_ascii=False, indent=2)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.astimezone(TAIPEI).strftime("%Y-%m-%d")
    day_path = os.path.join(OUTPUT_DIR, f"{today_str}.json")

    all_items = []
    any_success = False
    for feed in FEEDS:
        items = fetch_feed(feed)
        if items is not None:
            any_success = True
            all_items.extend(items)

    if not any_success:
        # 全部來源都失敗：若今天已有檔案就標記 stale 並保留，否則不新增今天的檔案
        if os.path.exists(day_path):
            with open(day_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing["stale"] = True
            with open(day_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        return

    # 依標題去重（同一則新聞常被多分類重複收錄），再依時間新到舊排序
    seen_titles = set()
    deduped = []
    all_items.sort(key=lambda x: x["pubDate"], reverse=True)
    for it in all_items:
        if it["title"] in seen_titles:
            continue
        seen_titles.add(it["title"])
        it["relatedStocks"] = related_stocks_for(it["title"])
        deduped.append(it)

    out = {
        "date": today_str,
        "generatedAt": now_utc.isoformat(),
        "stale": False,
        "items": deduped[:MAX_ITEMS],
        "note": "來源：Yahoo股市 RSS（國際財經／台股）自動彙整最新標題並依關鍵字關聯持股，非人工編選，僅供參考，不構成投資建議。",
    }
    with open(day_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    dates = load_index()
    if today_str not in dates:
        dates.append(today_str)
    save_index(dates)


if __name__ == "__main__":
    main()
