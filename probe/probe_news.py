# 一次性探測：Google 新聞原始 RSS 的來源網域與日期、各家媒體 RSS 的可用性與日期、Google 新聞連結解碼可行性
import json, re, time, html, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import timezone, timedelta
TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "zh-TW,zh;q=0.9"}
def get(url, data=None, headers=None, timeout=25):
    h = dict(UA); h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.geturl(), r.read()
def pd(s):
    try: return parsedate_to_datetime(s).astimezone(TZ).isoformat(timespec="minutes")
    except Exception: return s
report = {"gnews": {}, "feeds": {}, "decode": [], "bing": {}}
GN = "https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
for q in ["世芯", "穩懋", "奇鋐", "光寶科", "合晶", "大立光", "國巨", "高力", "PCB", "ABF 載板", "CCL 銅箔基板", "矽晶圓", "CPO 矽光子"]:
    try:
        st, _, raw = get(GN.format(q=urllib.parse.quote(f"{q} when:7d")))
        root = ET.fromstring(raw); items = []
        for it in root.findall(".//item"):
            src = it.find("source")
            items.append({"title": html.unescape(it.findtext("title") or ""), "pub": pd(it.findtext("pubDate") or ""),
                          "source": (src.text if src is not None else ""), "srcUrl": (src.get("url") if src is not None else ""),
                          "link": it.findtext("link")})
        report["gnews"][q] = items
    except Exception as e:
        report["gnews"][q] = f"ERR {e}"
    time.sleep(1.2)
FEEDS = [
 "https://technews.tw/feed/", "https://technews.tw/category/semiconductor/feed/", "https://technews.tw/category/component/feed/",
 "https://news.ltn.com.tw/rss/business.xml", "https://news.ltn.com.tw/rss/all.xml",
 "https://money.udn.com/rssfeed/lists/1001",
 "https://money.udn.com/rssfeed/news/1001/5588?ch=money", "https://money.udn.com/rssfeed/news/1001/5590?ch=money",
 "https://money.udn.com/rssfeed/news/1001/5591?ch=money", "https://money.udn.com/rssfeed/news/1001/5612?ch=money",
 "https://money.udn.com/rssfeed/news/1001/11074?ch=money", "https://money.udn.com/rssfeed/news/1001/10846?ch=money",
 "https://www.moneydj.com/KMDJ/RssCenter.aspx?svc=NR&fno=1&arg=X0000000",
 "https://feeds.feedburner.com/rsscna/finance", "https://www.cna.com.tw/rss/aie.aspx", "https://www.cna.com.tw/rss/afe.aspx",
 "https://www.ctee.com.tw/feed", "https://www.ctee.com.tw/rss_web/livenews/ctee", "https://www.ctee.com.tw/rss_web/livenews/industry",
 "https://tw.stock.yahoo.com/rss?category=news", "https://tw.stock.yahoo.com/rss?s=3661", "https://tw.stock.yahoo.com/rss?s=3661.TW",
 "https://www.digitimes.com.tw/rss/news.xml", "https://www.businesstoday.com.tw/rss", "https://www.wealth.com.tw/rss",
 "https://api.cnyes.com/media/api/v1/newslist/category/headline?limit=5", "https://api.cnyes.com/media/api/v1/newslist/category/tw_quo?limit=5",
 "https://api.cnyes.com/media/api/v1/newslist/category/tech?limit=5", "https://api.cnyes.com/media/api/v1/newslist/category/industry?limit=5",
 "https://api.cnyes.com/media/api/v1/newslist/category/tw_macro?limit=5",
]
for u in FEEDS:
    try:
        st, final, raw = get(u)
        info = {"status": st, "final": final, "bytes": len(raw)}
        txt = raw.decode("utf-8", "replace")
        if u.startswith("https://api.cnyes.com"):
            j = json.loads(txt); data = (j.get("items") or {}).get("data") or []
            info["sample"] = [{"title": d.get("title"), "pub": d.get("publishAt"), "cat": d.get("categoryName")} for d in data[:5]]
            info["total"] = (j.get("items") or {}).get("total")
        elif "<rss" in txt[:2000] or "<feed" in txt[:2000] or "<?xml" in txt[:100]:
            root = ET.fromstring(raw); its = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
            info["count"] = len(its)
            info["sample"] = [{"title": (i.findtext("title") or i.findtext("{http://www.w3.org/2005/Atom}title") or "")[:80],
                               "pub": pd(i.findtext("pubDate") or i.findtext("{http://www.w3.org/2005/Atom}updated") or ""),
                               "link": i.findtext("link")} for i in its[:6]]
        else:
            info["head"] = txt[:300]
            info["rsslinks"] = sorted(set(re.findall(r'https?://money\.udn\.com/rssfeed/news/[^"\'<> ]+', txt)))[:80]
        report["feeds"][u] = info
    except Exception as e:
        report["feeds"][u] = f"ERR {e}"
    time.sleep(0.5)
# Bing News RSS
for q in ["世芯", "光寶科 矽光子", "ABF 載板"]:
    try:
        st, final, raw = get(f"https://www.bing.com/news/search?q={urllib.parse.quote(q)}&format=rss&mkt=zh-TW&setlang=zh-hant")
        root = ET.fromstring(raw); its = root.findall(".//item")
        report["bing"][q] = [{"title": i.findtext("title"), "pub": pd(i.findtext("pubDate") or ""), "link": i.findtext("link"),
                              "source": i.findtext("{http://www.bing.com/news/search}Source") or ""} for i in its[:15]]
    except Exception as e:
        report["bing"][q] = f"ERR {e}"
    time.sleep(1)
# Google News 連結解碼（batchexecute）＋讀文章頁日期，挑自由時報與其他來源各幾則
def decode(link):
    m = re.search(r"/articles/([^?]+)", link); aid = m.group(1)
    st, final, raw = get(f"https://news.google.com/rss/articles/{aid}")
    page = raw.decode("utf-8", "replace")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page); ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts): return {"err": "no sg/ts", "final": final, "head": page[:200]}
    payload = ["Fbv4je", f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{ts.group(1)},"{sg.group(1)}"]']
    body = urllib.parse.urlencode({"f.req": json.dumps([[payload]])}).encode()
    st, _, raw = get("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    txt = raw.decode("utf-8", "replace")
    m = re.search(r'\[\\"garturlres\\",\\"(.*?)\\"', txt)
    return {"url": m.group(1).encode().decode("unicode_escape") if m else None, "raw": None if m else txt[:300]}
def page_date(url):
    st, final, raw = get(url)
    t = raw.decode("utf-8", "replace")
    out = {}
    for pat, k in [(r'property="article:published_time"\s+content="([^"]+)"', "metaPublished"),
                   (r'"datePublished"\s*:\s*"([^"]+)"', "jsonld"),
                   (r'<time[^>]*datetime="([^"]+)"', "timeTag"),
                   (r'img\.ltn\.com\.tw/Upload/[^"\']*?/(20\d\d/\d\d/\d\d)/', "ltnImg"),
                   (r'class="time"[^>]*>\s*([^<]{8,30})<', "ltnTime")]:
        m = re.search(pat, t)
        if m: out[k] = m.group(1)
    return out
cands = []
for q, items in report["gnews"].items():
    if isinstance(items, list):
        for it in items:
            if it["source"] == "自由時報" and len([c for c in cands if c["source"] == "自由時報"]) < 6: cands.append(it)
            elif it["source"] in ("經濟日報", "聚財網", "富聯網", "工商時報") and len([c for c in cands if c["source"] == it["source"]]) < 2: cands.append(it)
for it in cands:
    rec = {"title": it["title"][:60], "source": it["source"], "srcUrl": it["srcUrl"], "gnewsPub": it["pub"]}
    try:
        d = decode(it["link"]); rec.update(d)
        if d.get("url"):
            try: rec["page"] = page_date(d["url"])
            except Exception as e: rec["pageErr"] = str(e)
    except Exception as e:
        rec["decodeErr"] = str(e)
    report["decode"].append(rec); time.sleep(1.5)
json.dump(report, open("probe/report.json", "w"), ensure_ascii=False, indent=1)
print("done")
