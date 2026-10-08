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
    "2049": "上銀科技", "6805": "富世達", "2464": "盟立自動化", "7828": "創新服務 7828",
}


# ---------------------------------------------------------------- 雜訊過濾
# 不是新聞的內容一律排除：網友討論區（CMoney 股市爆料同學會、LINE TODAY 討論牆）、CMoney 自動產生的
# 個股頁（個股概覽、股價預估）、影音／社群貼文、AI 問答頁、只列一串股票與數字的排行榜。
# 2026-10-08 第一次全面抓取時 549 則裡有 156 則是爆料同學會貼文，把真正的新聞擠出每檔 8 則的名額。
NOISE_TITLE = re.compile(
    r"股市爆料同學會|爆料同學會|討論牆|討論區|PTT|Dcard|Mobile01"
    r"|個股概覽|股價預估|怎麼買賣|今日股價與討論|金包銀之|即時新聞|即時報價|股價走勢與"  # CMoney 盤中股價快訊、報價頁
    r"|排行榜|TOP\s*\d+|前\d+名|\d+大成交|【[^】]*(?:節目|直播)[^】]*】|#[^\s#]"  # 帶 hashtag 的是影音／社群貼文
    r"|^[^\s（(]{2,8}[（(]\d{4}[)）]$|^\d{4}\s*\S{1,8}$"  # 只有「松川精密(7788)」「3661世芯KY」這樣的個股頁
    r"|權證|(?<!增資)(?<!員工)(?<!現增)認(?:購|售)|牛熊證|最牛一輪|即時走勢"  # 權證推薦專欄、權證報價頁
    r"|取得.{0,16}(?:設備|廠務工程|營業使用|供營業用|營業用之)|海外路演|NDR"  # 例行重訊的轉載（重訊卡片已列在例行公告）
    r"|[\U0001F300-\U0001FAFF\u2600-\u27BF]"  # 標題帶表情符號的幾乎都是社群貼文
)
# 時報資訊「《投信買超5-4》景碩(151)、奇鋐(147)…」「《上週上櫃成交金額排名（6-3）》」這類數字表
TABLE_TITLE = re.compile(r"《[^》]*\d+-\d+[)）]?》")
KEEP_TABLE = re.compile(r"處置|注意股|管制")  # 處置股／注意股名單會影響交易方式，保留
NOISE_SOURCES = {"YouTube", "Facebook", "Instagram", "Threads", "Dcard", "PTT", "Mobile01", "鉅亨號", "CMoney投資網誌"}  # 鉅亨號、CMoney投資網誌是網友／投資達人部落格
QA_SOURCES = {"UDN", "CMoney"}  # 這兩個來源以問句結尾的標題多半是自動產生的問答頁（例：「金像電毛利率預估多少？」）


def is_noise(title, source):
    title = title or ""
    source = (source or "").strip()
    if source in NOISE_SOURCES or NOISE_TITLE.search(title) or NOISE_TITLE.search(source):
        return True
    if TABLE_TITLE.search(title) and not KEEP_TABLE.search(title):
        return True
    return source in QA_SOURCES and bool(re.search(r"[？?]\s*$", title))


# ---------------------------------------------------------------- 新聞類型（只保留使用者要的）
# 使用者 2026-10-08：「營收財報不用發」、「主要是需要新聞資訊，例如法說內容、重大影響營運結構的事件、
# 未來展望、產業趨勢這類型的」。依標題判斷，只收三類：
#   event   重大事件：檢調／訴訟／災害／罷工等突發事件，以及併購、增資、合資、擴產建廠、大額投資、重大訂單
#   outlook 法說／展望：法說會、股東會、經營層發言，或標題對後市的看法（明年、Q4、旺季、看好…）
#   trend   產業趨勢：漲價缺貨、供需、產品技術世代、量產送樣、訂單客戶、AI／記憶體／載板等產業動態
# 不收（回傳 None）：月營收與財報數字、股價漲跌與盤勢、法人買賣超與目標價、注意／處置股、投資建議文。
# 被排除的新聞不佔每檔 8 則的名額。
_AMOUNT = r"\s*[\d一二三四五六七八九十百千萬億.,]+\s*(?:億|萬)"
NEWS_HYPE = re.compile(  # 投資建議、喊價文：不管講什麼都不收
    r"撿便宜|可以追|能追|追嗎|追高|搶進|卡位|攻略|閉眼買|口袋名單|又要噴|飆股|妖股|買點|存股|上車|下車|誰還沒漲|必買|快跑|CP值|狂喊"
    r"|能買|可買|該買|要買|怎麼買|能抱|續抱")
NEWS_EVENT = re.compile(
    r"搜索|調查局|檢調|約談|起訴|羈押|交保|收押|內線|掏空|訴訟|裁罰|罰鍰|違約|跳票|資安事件|遭駭|駭客攻擊|網路攻擊|勒索"
    r"|火災|火警|氣爆|爆炸|停工|停產|罷工|下市|澄清|重訊"
    r"|併購|收購|合併案|吸收合併|入股|增資|私募|減資|分割|合資|結盟|策略聯盟|出售|處分|關廠|裁員|換帥|接班"
    r"|擴產|擴廠|新廠|建廠|設廠|買地|購地|(?<!盤)大單|拿下|打入(?!處置|注意|跌停|漲停|全額)|打進(?!處置|注意|跌停|漲停)|獨家|轉單"
    rf"|(?:砸|斥資|投資|資本支出|擲){_AMOUNT}.{{0,12}}(?:廠|設備|產能|土地|園區|買下|購入|入股)")
NEWS_CALL = re.compile(r"法說|法人說明會|股東會")
NEWS_OUTLOOK_STRONG = re.compile(r"法說|法人說明會|股東會|展望|董座|董事長|總經理|執行長|總裁|營運長|財務長|發言人|CEO|CFO|COO")
# 具體的產業資訊：TREND_KEY 是漲價缺貨、量產認證、技術規格這類「產業本身的變化」；TREND_BIZ（訂單、客戶、拉貨）
# 常被拿來解釋月營收，所以標題主軸是營收數字時，只有 TREND_KEY 才算數
NEWS_TREND_KEY = re.compile(
    r"漲價|跌價|降價|傳漲|喊漲|調漲|缺貨|供不應求|吃緊|供過於求|合約價|(?<!即時)報價|供需|庫存|稼動率|產能利用率|滿載|量產|送樣|認證|驗證"
    r"|新品|新世代|規格|製程|改採|專利|市占|搶光|交期")
NEWS_TREND_BIZ = re.compile(r"訂單|接單|客戶|拉貨|擴充")
NEWS_REVENUE = re.compile(r"營收|EPS|每股|獲利|毛利|淨利|盈餘|自結|稅後|純益|財報|季增|年增|月增|月減|年減")
NEWS_MOVE = re.compile(
    r"漲停|跌停|大漲|大跌|上漲|下跌|亮燈|噴|飆|重挫|下挫|勁揚|摜|跳水|強攻|走強|走弱|拉回|盤中|領漲|領跌|爆量|攻頂|翻黑|翻紅|翻綠|撐紅"
    r"|收漲|收跌|收紅|收黑|千金股|狂殺|急殺|狂瀉|天價|股價|股王|股后|市值|走勢|漲幅|跌幅|漲逾|跌逾|漲\d|跌\d|即時新聞"
    r"|開盤|收盤|早盤|尾盤|均線|跌破|站上|站回|成交額|成交量|多頭|空頭|多方|空方|交易活躍")
NEWS_CHIPS = re.compile(r"外資|投信|自營商|三大法人|法人|買超|賣超|融資|融券|借券|籌碼|主力|ETF|00\d{3}|目標價|評等|喊買|喊進|喊賣|國家隊")
NEWS_DISPOSAL = re.compile(r"注意股|處置|管制|撮合|警示")
NEWS_OUTLOOK_WEAK = re.compile(r"明年|下半年|上半年|第四季|第4季|Q4|4Q\d\d|後市|看好|看旺|看淡|看俏|轉弱|轉強|樂觀|保守|審慎|動能|旺季|淡季|成長可期|續旺")
NEWS_TREND_WEAK = re.compile(
    r"AI|CPO|HBM|ASIC|GPU|伺服器|資料中心|液冷|散熱|載板|ABF|CCL|PCB|矽光子|光通訊|記憶體|DRAM|NAND|晶圓|封裝|CoWoS|衛星|機器人|電動車"
    r"|800G|1\.6T|3\.2T|FAU|商機|市場|題材|供應鏈|布局|佈局|轉型|技術|需求|產能")


def news_category(title):
    t = title or ""
    if NEWS_HYPE.search(t):
        return None
    revenue = bool(NEWS_REVENUE.search(t))
    strong_trend = bool(NEWS_TREND_KEY.search(t)) or (not revenue and bool(NEWS_TREND_BIZ.search(t)))
    move = bool(NEWS_MOVE.search(t))
    if NEWS_CALL.search(t) and (strong_trend or not move):
        return "outlook"  # 法說會內容即使有 EPS、毛利率數字也要收；「法說前股價熄火」這種盤勢文不算
    if NEWS_EVENT.search(t):
        return "event"
    # 董座／總經理發言；標題主軸是營收數字的（「營收創新高董座開講」）不算
    if NEWS_OUTLOOK_STRONG.search(t) and not revenue and (strong_trend or not move):
        return "outlook"
    if strong_trend:
        return "trend"  # 標題有具體的產業資訊（漲價、缺貨、量產、訂單…），即使順帶提到股價或法人也收
    if revenue or move or NEWS_CHIPS.search(t) or NEWS_DISPOSAL.search(t):
        return None
    if NEWS_OUTLOOK_WEAK.search(t):
        return "outlook"
    if NEWS_TREND_WEAK.search(t):
        return "trend"
    return None


# 標題尾巴常掛著網站分類（「| 科技產業| 產經」「- 日報」「｜新聞快訊｜豐雲學堂」），拿掉比較好讀
TITLE_SUFFIX = re.compile(
    r"(?:\s*[|｜]\s*[^|｜]{1,14}"
    r"|\s*[-－]\s*(?:日報|上市櫃|產業|新聞|台股|證券|財經|產經|科技|要聞|焦點|股市|理財|個股|市場焦點|國際|兩岸"
    r"|財經要聞|工商時報|經濟日報|中時新聞網|即時新聞|即時))+\s*$"
)


def clean_title(title):
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    stripped = TITLE_SUFFIX.sub("", t).strip()
    return stripped if len(stripped) >= 8 else t


# ---------------------------------------------------------------- 標題比對：這則新聞是不是在講這檔股票
# 原本只看「標題有沒有出現簡稱」，2026-10-08 檢查發現：創意(3443) 8則有6則是「創意築夢特教美展」這類，
# 大量(3167) 8則有6則是「購入大量輝達晶片」，全新、新代（「新代幣」）、南亞（「南亞科技」「東南亞」）、
# 群創／群聯（「族群創高」「族群聯袂」）、力成（「實力成長」）也會誤中。

def _ambiguous(short, full, words=""):
    """簡稱是常見詞：標題要寫全名，或簡稱後面直接接代號／月份／季度／冒號／股市用語，
    或夾在頓號、逗號列出的股票清單中間（「漲停、創意、健策再創新高」）。"""
    after = r"\s*[（(]|\s*\d+\s*月|\s*Q[1-4]|[：:]"
    if words:
        after += rf"|\s*(?:{words})"
    sep = r"、，,／/.．·‧"  # 股票清單的分隔符號（「全新.創意.華星光等自由了」）
    return (rf"{full}|{short}(?={after})"
            rf"|(?<=[{sep}」])\s*{short}(?=\s*[{sep}])|^{short}(?=[{sep}])")


STOCK_WORDS = r"營收|業績|股價|漲停|跌停|大漲|大跌|攻(?:頂|高|上|漲停)|飆|衝(?:高|上|破)|再創|創(?:新?高|歷史)|法說|獲利|EPS|目標價"

TITLE_PATTERNS = {
    "2455": _ambiguous("全新", "全新光電", STOCK_WORDS + "|同步"),
    "3167": _ambiguous("大量", "大量科技", r"營收|股價|漲停|跌停"),  # 「大量出貨」「大量訂單」很常見，不收
    "3443": _ambiguous("創意", "創意電子", STOCK_WORDS + "|ASIC"),
    "7750": _ambiguous("新代", "新代科技", STOCK_WORDS),
    "7828": r"創新服務(?=\s*[（(]\s*7828)",  # 「創新服務」是常見詞，只認「創新服務(7828)」這種寫法
    "1303": r"南亞塑膠|(?<!東)南亞(?!科|電路|洲|太平洋|國|市場|地區|區|諸|次大陸|各國|人|裔|海|航)",
    "3481": r"(?<![族社集人])群創",
    "8299": r"(?<![族社集人])群聯",
    "6239": r"(?<![努能實潛動壓活魅戰火電馬體財權助產人競])力成(?![長為功本績果熟形真])",
    "3529": r"(?<![動活買人氣能實潛戰火電馬財])力旺(?!盛)",
    "6531": r"愛普(?!生|羅)",  # 愛普生＝Epson
    "2049": r"上銀(?!行|色|牌)",
    "3081": r"聯亞(?!藥|生)",
    "4576": r"大銀微系統|大銀(?!行|河|幕|髮|色)",
    "2313": r"(?<!中)華通(?!訊|白銀)",  # 「華通白銀」是中國白銀報價
    "6510": r"中華精測|(?<!武漢)精測(?!電子|轉債)",  # 「精測電子」「精測轉債」是中國同名公司
    "2464": r"(?<!聯)盟立",  # 「聯盟立法」
    "8996": r"(?<![提升拉調])高力(?!士|量|度|道|氣)",
    "8046": r"(?<![東西])南電",
}
# 標題常用、但跟網站上的名稱不同的寫法
EXTRA_ALIASES = {
    "6739": ["竹陞"], "6683": ["雍智"], "2449": ["京元電"], "6672": ["騰輝"],
    "3711": ["日月光"], "2301": ["光寶"],
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


# 重大訊息分級（網站依此排序、標色，早上的 Claude 每日彙整也參考）：
# 2 = 重大事件：檢調搜索、停工停產、火災等災害、違約跳票、重大損失、下市或停止買賣、主管機關裁罰、資安攻擊
# 1 = 需要留意：澄清媒體報導、增減資、私募、併購、庫藏股、財測、經營層異動、處置／注意交易、訴訟、地震影響
# 0 = 例行公告：取得設備或廠務工程、月營收、法說會／海外路演、背書保證、公司債收足款項…
ANN_LEVEL2 = re.compile(
    r"搜索|調查局|檢調|約談|起訴|羈押|拘提|火災|火警|爆炸|氣爆|停工|停產|停電|災害|淹水|違約|退票|跳票"
    r"|重大損失|鉅額損失|虧損達|下市|停止買賣|暫停交易|終止上市|全額交割|裁罰|罰鍰|勒令|網路攻擊|駭客|資安事件|勒索")
ANN_LEVEL1 = re.compile(
    r"澄清|報導|傳聞|減資|現金增資|私募|吸收合併|合併案|合併基準日|簡易合併|收購|股份轉換|分割|庫藏股|買回本公司股份"
    r"|財務預測|董事長|總經理|執行長|辭任|解任|異動|處置|注意交易|重編|繼續經營|變更交易方法|訴訟|仲裁|假扣押|減損|虧損|地震")


def ann_level(title):
    if ANN_LEVEL2.search(title or ""):
        return 2
    if ANN_LEVEL1.search(title or ""):
        return 1
    return 0


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
            title = clean_title(html.unescape(str(it["title"])))
            if is_noise(title, "鉅亨網"):
                continue
            ts = it.get("publishAt")
            published = datetime.fromtimestamp(ts, TZ).isoformat(timespec="minutes") if ts else None
            items.append({
                "codes": hit,
                "title": title,
                "source": "鉅亨網",
                "url": f"https://news.cnyes.com/news/id/{it['newsId']}",
                "publishedAt": published,
                "via": "cnyes",
            })
        time.sleep(0.5)
    return items


# ---------------------------------------------------------------- 3. Google 新聞（逐檔搜尋）
def fetch_google_news(code, name, matcher):
    short = short_name(name)
    query = QUERY_OVERRIDES.get(code) or short
    url = GNEWS_URL.format(q=urllib.parse.quote(f"{query} when:{NEWS_KEEP_DAYS}d"))
    root = ET.fromstring(http_get(url, retries=1))
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
        title = clean_title(title)
        # 標題要真的在講這檔股票，排除只在內文順帶一提、或簡稱剛好是常見詞的新聞
        if not matcher.search(title) or is_noise(title, source):
            continue
        try:
            published = parsedate_to_datetime(pub).astimezone(TZ).isoformat(timespec="minutes")
        except (TypeError, ValueError):
            published = None
        out.append({"codes": [code], "title": title, "source": source or "Google 新聞",
                    "url": link, "publishedAt": published, "via": "google"})
    return out


# ---------------------------------------------------------------- 標題點名多檔股票時一併標註
def _literal(alias):
    esc = re.escape(alias)
    # 英文簡稱（IET）要整個字比對，避免撞到其他英文字
    return rf"(?<![A-Za-z]){esc}(?![A-Za-z])" if re.fullmatch(r"[A-Za-z0-9\-]+", alias) else esc


def build_title_matchers(tracked):
    matchers = {}
    for code, name in tracked.items():
        if code in TITLE_PATTERNS:
            alts = [TITLE_PATTERNS[code]]
            if QUERY_OVERRIDES.get(code):
                alts.append(re.escape(QUERY_OVERRIDES[code]))
        else:
            names = {short_name(name), QUERY_OVERRIDES.get(code), *EXTRA_ALIASES.get(code, [])} - {None, ""}
            alts = [_literal(a) for a in sorted(names, key=len, reverse=True)]
        # 代號只認括號寫法「創意(3443)」「力成(6239-TW)」，避免撞到金額、張數等數字
        alts.append(rf"[（(]\s*{code}(?:-TW)?\s*[)）]")
        matchers[code] = re.compile("|".join(f"(?:{a})" for a in alts))
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
    for a in announcements:
        a["level"] = ann_level(a.get("title"))  # 每次重算，分級規則調整後舊資料也會跟著更新

    # 2) + 3) 個股新聞
    matchers = build_title_matchers(tracked)
    fresh = []
    try:
        cn = fetch_cnyes(tracked)
        fresh.extend(cn)
        status["cnyes"] = f"ok（{len(cn)}則與追蹤股票相關）"
    except Exception as e:
        status["cnyes"] = f"失敗：{e}"
        print(f"::warning::鉅亨網新聞抓取失敗：{e}")

    g_ok = g_fail = g_items = 0
    g_blocked = False
    for code, name in tracked.items():
        if g_blocked:
            break
        try:
            got = fetch_google_news(code, name, matchers[code])
            fresh.extend(got)
            g_items += len(got)
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
    status["google"] = f"成功 {g_ok} 檔、失敗 {g_fail} 檔、取得 {g_items} 則" + ("（中途被限流）" if g_blocked else "")

    tag_codes(fresh, matchers)

    cutoff_news = (now - timedelta(days=NEWS_KEEP_DAYS)).isoformat(timespec="minutes")
    old_news = old.get("news") or {}
    news = {}
    for code in tracked:
        pool = []
        for it in old_news.get(code, []):
            # 舊資料用目前的規則重新檢查：討論區貼文、雜訊、同名誤判（Google 新聞的標題沒提到這檔）一併清掉；
            # 鉅亨網的文章是發布者自己標註的個股，標題沒寫股名也算數
            it = dict(it, title=clean_title(it.get("title")))
            if is_noise(it["title"], it.get("source")):
                continue
            if it.get("via") == "google" and not matchers[code].search(it["title"]):
                continue
            pool.append(it)
        for it in fresh:
            if code in it["codes"]:
                pool.append({k: v for k, v in it.items() if k != "codes"})
        seen = set()
        kept = []
        for it in sorted(pool, key=lambda x: x.get("publishedAt") or "", reverse=True):
            if (it.get("publishedAt") or "") < cutoff_news:
                continue
            cat = news_category(it.get("title"))
            if not cat:
                continue  # 營收數字、股價漲跌、籌碼目標價這類不收（見 NEWS_RULES）
            key = norm_title(it.get("title"))[:40]
            if not key or key in seen or it.get("url") in seen:
                continue
            seen.add(key)
            seen.add(it.get("url"))
            kept.append(dict(it, cat=cat))
            if len(kept) >= NEWS_PER_STOCK:
                break
        if kept:
            news[code] = kept

    output = {
        "generatedAt": now.isoformat(timespec="seconds"),
        "note": "公司重大訊息來自證交所／櫃買中心 OpenAPI（公開資訊觀測站每日重大訊息）；個股新聞來自鉅亨網台股新聞（文章標註個股）與 Google 新聞搜尋，僅保存標題與連結作為新聞索引，並只保留重大事件（event）、法說／展望（outlook）、產業趨勢（trend）三類。重大訊息保留近30天、個股新聞保留近7天。",
        "sources": status,
        "announcements": announcements,
        "news": news,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"重大訊息 {len(announcements)} 則（本次新抓 {len(anns)} 則）、個股新聞 {sum(len(v) for v in news.values())} 則（{len(news)} 檔有新聞）")
    print(json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    main()
