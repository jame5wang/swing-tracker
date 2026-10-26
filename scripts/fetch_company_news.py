"""
抓取「追蹤股票」的公司重大訊息、個股新聞與產業新聞，寫入 data/company_news.json

為什麼要有這支腳本：
原本的新聞只抓 Yahoo股市「國際／台股」兩個 RSS 分類（每天一次、最多12則），幾乎都是大盤與美股
盤後新聞，沒有任何個股層級的消息，也沒有公開資訊觀測站的重大訊息——2026-10-07 奇鋐、富世達
「新莊廠遭調查局搜索」的重訊就完全沒被收錄。這支腳本補上個股與產業層級的來源：

1. 公司重大訊息（官方）：證交所 OpenAPI /opendata/t187ap04_L（上市公司每日重大訊息）、
   櫃買中心 OpenAPI /mopsfin_t187ap04_O（上櫃公司每日重大訊息），只保留追蹤股票。
2. 鉅亨網新聞 API：台股新聞附有發布者標註的相關個股代號；科技類新聞用標題比對股名與產業關鍵字。
3. 各家媒體官方 RSS（科技新報、經濟日報、工商時報、自由財經、中央社、DIGITIMES、Yahoo股市）：
   發布時間由媒體提供、可信，用標題比對 72 檔股名與產業關鍵字。
4. Google 新聞 RSS：72 檔逐檔以股名（含常見全名／別名）搜尋，19 個產業逐一以產業關鍵字
   （PCB、ABF載板、CCL、CPO…，2026-10-26 使用者要求加入）搜尋近7天新聞。
   Google 新聞會把舊文章重新標上今天的日期（見 DateChecker），一律讀文章頁面上的發布時間驗證。
   （只保存標題、來源、時間與連結，作為個人觀察筆記的新聞索引，不轉載內文。）

每個來源各自獨立：某個來源抓取失敗時，沿用上一次的資料，不會把已有的新聞清空。
重大訊息保留近30天、個股新聞保留近7天（每檔最多8則）、產業新聞保留近7天（每個產業最多8則），滾動更新。
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
CNYES_CAT_URL = "https://api.cnyes.com/media/api/v1/newslist/category/{cat}?limit=100&page={page}"
CNYES_EXTRA_CATS = ("tech",)  # 科技類新聞（產業動態多），用標題比對股名與產業關鍵字
CNYES_PAGES = 3
GNEWS_URL = "https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
MOPS_LINK = "https://mops.twse.com.tw/mops/#/web/t05sr01_1"

ANN_KEEP_DAYS = 30
NEWS_KEEP_DAYS = 7
NEWS_PER_STOCK = 8
GNEWS_PAUSE = 1.2  # 每次 Google 新聞查詢之間的間隔（秒），避免被限流
INDUSTRY_PER_SECTOR = 8

# 各家媒體官方 RSS：發布時間由媒體自己提供、可信，用標題比對 72 檔股名與產業關鍵字
FEEDS = [
    ("科技新報", "https://technews.tw/feed/"),
    ("科技新報", "https://technews.tw/category/semiconductor/feed/"),
    ("科技新報", "https://technews.tw/category/component/feed/"),
    ("自由財經", "https://news.ltn.com.tw/rss/business.xml"),
    ("經濟日報", "https://money.udn.com/rssfeed/news/1001/5591?ch=money"),
    ("工商時報", "https://www.ctee.com.tw/rss_web/livenews/industry"),
    ("工商時報", "https://www.ctee.com.tw/rss_web/livenews/ctee"),
    ("中央社", "https://feeds.feedburner.com/rsscna/finance"),
    ("DIGITIMES", "https://www.digitimes.com.tw/rss/news.xml"),
    ("Yahoo股市", "https://tw.stock.yahoo.com/rss?category=news"),
]

# ---- Google 新聞的日期驗證
# 2026-10-26 使用者發現：世芯「法說會速報》看好下半年營收大增」是3月的文章、光寶科「股東會速報》矽光子2028年
# 貢獻營收」是5月的文章，網站卻顯示「8小時前」。查證後發現 Google 新聞會把自由時報股市網（stock.ltn.com.tw）
# 的舊文章重新收錄並給今天的日期（抽查6則全是3～9月的舊文），搜尋條件 when:7d 也擋不掉。
# 處理方式：把 Google 新聞的轉址連結解回原始網址、讀文章頁面上的發布時間（JSON-LD、meta、<time>、
# 自由時報的圖片上傳日期），以文章本身的日期為準；結果存在 data/news_dates.json，同一則只驗證一次。
DATES_PATH = "data/news_dates.json"
DATES_KEEP_DAYS = 60
VERIFY_MAX = 450      # 每次執行最多驗證幾則（第一次要補驗舊資料，之後每次只驗新出現的）
VERIFY_PAUSE = 0.25
UNRELIABLE_SITES = ("stock.ltn.com.tw",)  # Google 日期不可信：驗證不到真實日期就不收
UNRELIABLE_SOURCES = {"自由時報"}        # 舊資料沒有記網域時用來源名稱判斷（Google 新聞裡的「自由時報」＝stock.ltn.com.tw）
GN_ARTICLE_URL = "https://news.google.com/rss/articles/{aid}"
GN_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

# ---- 產業關鍵字（使用者 2026-10-26：「產業也要納入，例如 PCB、ABF、CCL」）
# key 要跟網站 SEED_STOCKS 的 sector 名稱一致（新增產業沒設定時，直接用產業名稱搜尋）；
# 第一個值是 Google 新聞搜尋詞（OR 串接），第二個值是標題比對規則（標題沒提到產業關鍵字的不收）
INDUSTRY_KEYWORDS = {
    "ABF載板": (["ABF載板", "IC載板", "載板", "玻璃基板"], r"ABF|IC載板|載板|玻璃基板"),
    "CCL／覆銅板": (["CCL", "銅箔基板", "覆銅板", "玻纖布", "HVLP", "石英布"],
                 r"(?<![A-Za-z])CCL(?![A-Za-z])|銅箔基板|覆銅板|玻纖布|玻纖紗|石英布|HVLP|高速材料|Low ?Dk|M9(?:材料|等級|CCL)"),
    "PCB／軟板": (["PCB", "印刷電路板", "軟板", "HDI"],
                r"(?<![A-Za-z])PCB(?![A-Za-z])|印刷電路板|電路板|軟板|(?<![A-Za-z])HDI(?![A-Za-z])|高層數板|伺服器板|mSAP|鑽針"),
    "矽晶圓": (["矽晶圓", "再生晶圓", "SOI晶圓"], r"矽晶圓|再生晶圓|磊晶|(?<![A-Za-z])SOI(?![A-Za-z])"),
    "封測": (["封測", "先進封裝", "CoWoS", "面板級封裝", "CoPoS"],
           r"封測|先進封裝|CoWoS|CoWoP|CoPoS|SoIC|面板級封裝|FOPLP|扇出型|封裝產能|測試產能"),
    "晶圓代工": (["晶圓代工", "成熟製程"], r"晶圓代工|成熟製程|8吋廠|12吋廠|特殊製程|代工價格|代工報價"),
    "記憶體": (["DRAM", "NAND", "HBM", "記憶體"],
            r"記憶體|DRAM|NAND|(?<![A-Za-z])HBM|DDR[345]|LPDDR|快閃記憶體|(?<![A-Za-z])SSD(?![A-Za-z])|SK海力士|美光|Micron|鎧俠|Kioxia|長鑫"),
    "面板": (["面板廠", "面板報價", "LCD", "OLED"], r"面板(?!級)|(?<![A-Za-z])LCD|OLED|Micro ?LED|Mini ?LED|車用顯示|顯示器"),
    "電源／工控": (["伺服器電源", "BBU", "HVDC", "電源供應器", "800V直流"],
               r"電源|(?<![A-Za-z])BBU|備援電池|HVDC|800 ?V|功率模組|工控|工業電腦|儲能|(?<![A-Za-z])SMT(?![A-Za-z])|繼電器"),
    "化合物半導體": (["化合物半導體", "砷化鎵", "氮化鎵", "碳化矽", "磷化銦"],
               r"化合物半導體|砷化鎵|GaAs|氮化鎵|(?<![A-Za-z])GaN(?![A-Za-z])|碳化矽|(?<![A-Za-z])SiC(?![A-Za-z])|磷化銦|(?<![A-Za-z])InP(?![A-Za-z])|VCSEL|功率放大器|射頻元件"),
    "散熱": (["散熱", "液冷", "水冷板", "均熱片"], r"散熱|液冷|水冷|冷板|均熱片|浸沒式|(?<![A-Za-z])CDU(?![A-Za-z])|分歧管|快接頭|熱管"),
    "IC設計/矽智財(SIP)": (["矽智財", "ASIC", "IC設計"], r"矽智財|(?<![A-Za-z])ASIC|IC設計|客製化晶片|設計服務|(?<![A-Za-z])IP(?:授權|廠|供應商)|晶片設計"),
    "自動化設備": (["自動化設備", "人形機器人", "工具機", "半導體設備"],
              r"自動化|機器人|機械手臂|工具機|線性滑軌|滾珠螺桿|傳動元件|減速機|運動控制|半導體設備|機器視覺|(?<![A-Za-z])AMR(?![A-Za-z])|(?<![A-Za-z])CNC(?![A-Za-z])"),
    "光通訊": (["CPO", "矽光子", "光通訊", "光收發模組", "共同封裝光學"],
             r"(?<![A-Za-z])CPO(?![A-Za-z])|共同封裝光學|矽光子|光通訊|光收發|光模組|光模塊|光纖|光耦合|(?<![A-Za-z])FAU(?![A-Za-z])|(?<![\d.])800G|1\.6T|3\.2T|光引擎|(?<![A-Za-z])EML(?![A-Za-z])|雷射晶片|光學鏡頭"),
    "稀有金屬回收": (["稀有金屬", "貴金屬回收", "靶材", "城市採礦"], r"稀有金屬|貴金屬回收|靶材|金屬回收|資源回收|城市採礦|稀土|關鍵礦產|電子廢料"),
    "被動元件": (["被動元件", "MLCC", "電容"], r"被動元件|MLCC|積層陶瓷|電容|電阻|電感|鉭質"),
    "測試設備": (["探針卡", "測試座", "半導體測試", "測試介面"],
              r"探針卡|測試座|測試介面|半導體測試|測試設備|測試機台|(?<![A-Za-z])ATE(?![A-Za-z])|老化測試|Burn-?in|分選機|光電同測|(?<![A-Za-z])SLT(?![A-Za-z])"),
    "連接器": (["連接器", "高速連接器", "CPU插槽"], r"連接器|銅纜|高速線纜|線束|CPU插槽|CPU插座"),
    "網通設備": (["交換器", "網通", "乙太網路"], r"交換器|網通|乙太網路|Ethernet|白牌|網路設備"),
}

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
# Google 新聞搜尋時另外用 OR 加上的常見寫法（2026-10-26 使用者要求 72 檔股名都要納入追蹤，這幾檔原本常常整週沒新聞）
GNEWS_EXTRA_TERMS = {
    "3529": ["力旺電子"], "6922": ["宸曜科技"], "6451": ["訊芯-KY"], "3532": ["台勝科技"], "4971": ["英特磊"],
    "6739": ["竹陞"], "2464": ["盟立"], "2449": ["京元電"], "6683": ["雍智"], "8021": ["尖點"],
    "6672": ["騰輝"], "3711": ["日月光"], "2301": ["光寶"], "7788": ["松川精密"], "2455": ["全新光電"],
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
    r"|取得.{0,16}(?:設備|廠務工程|營業使用|供營業用|營業用之)|海外路演|NDR|受邀參加"  # 例行重訊的轉載（重訊卡片已列在例行公告）
    r"|[\U0001F300-\U0001FAFF\u2600-\u27BF]"  # 標題帶表情符號的幾乎都是社群貼文
)
# 時報資訊「《投信買超5-4》景碩(151)、奇鋐(147)…」「《上週上櫃成交金額排名（6-3）》」這類數字表
TABLE_TITLE = re.compile(r"《[^》]*\d+-\d+[)）]?》")
KEEP_TABLE = re.compile(r"處置|注意股|管制")  # 處置股／注意股名單會影響交易方式，保留
NOISE_SOURCES = {"YouTube", "Facebook", "Instagram", "Threads", "Dcard", "PTT", "Mobile01", "鉅亨號", "CMoney投資網誌"}  # 鉅亨號、CMoney投資網誌是網友／投資達人部落格
QA_SOURCES = {"UDN", "CMoney"}  # 這兩個來源以問句結尾的標題多半是自動產生的問答頁（例：「金像電毛利率預估多少？」）


FOREIGN_SOURCE = re.compile(r"香港|Colliers|東網|東方日報|AM730|商業電台|Now 新聞|觀點網|HKBT|HKET|^Yahoo 財經$")  # 香港媒體上的同名公司（高力國際等）


def is_noise(title, source):
    title = title or ""
    source = (source or "").strip()
    if source in NOISE_SOURCES or NOISE_TITLE.search(title) or NOISE_TITLE.search(source) or FOREIGN_SOURCE.search(source):
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
# 公益、文教、得獎這類企業公關新聞，跟營運結構或產業趨勢無關
NEWS_PR = re.compile(r"文教|基金會|公益|捐贈|捐款|獎學金|淨灘|植樹|志工|頒獎|獲獎|得獎|榮獲|表揚|運動會|音樂會|藝文")
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
    if NEWS_HYPE.search(t) or NEWS_PR.search(t):
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
    "8996": r"(?<![提升拉調])高力(?!士|量|度|道|氣|國際|香港)",  # 「高力國際」是香港地產顧問 Colliers 的中文名
    "8046": r"(?<![東西])南電",
}
# 標題常用、但跟網站上的名稱不同的寫法
EXTRA_ALIASES = {
    "6739": ["竹陞"], "6683": ["雍智"], "2449": ["京元電"], "6672": ["騰輝"],
    "3711": ["日月光"], "2301": ["光寶"], "4971": ["英特磊"],
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


# ---------------------------------------------------------------- 日期工具
def parse_any_date(value):
    """把各種日期字串（RFC822、ISO、2026/10/23 13:52、2026-10-23T13:52:25+0800）轉成台北時間 ISO 字串。"""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return dt.astimezone(TZ).isoformat(timespec="minutes")
    except (TypeError, ValueError, IndexError):
        pass
    m = re.search(r"(20\d\d)\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})日?(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh, mm = int(m.group(4) or 0), int(m.group(5) or 0)
    tz = TZ
    if m.group(7):
        z = m.group(7)
        if z == "Z":
            tz = timezone.utc
        else:
            sign = 1 if z[0] == "+" else -1
            digits = z[1:].replace(":", "")
            tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0)))
    try:
        dt = datetime(y, mo, d, hh, mm, tzinfo=tz)
    except ValueError:
        return None
    return dt.astimezone(TZ).isoformat(timespec="minutes")


def site_of(url):
    try:
        return (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def strip_tags(text):
    return re.sub(r"<[^>]+>", "", text or "")


# ---------------------------------------------------------------- 2. 鉅亨網（文章標註個股＋科技類）
def fetch_cnyes(tracked):
    """回傳鉅亨網文章（codes＝發布者標註的追蹤股票，可能是空的；空的之後再用標題比對股名與產業關鍵字）。"""
    items = []
    pages = [("tw_stock", p) for p in range(1, CNYES_PAGES + 1)] + [(c, 1) for c in CNYES_EXTRA_CATS]
    for cat, page in pages:
        url = CNYES_CAT_URL.format(cat=cat, page=page)
        try:
            data = json.loads(http_get(url).decode("utf-8"))
        except Exception as e:
            if cat == "tw_stock" and page == 1:
                raise
            print(f"鉅亨網 {cat} 第{page}頁抓取失敗：{e}")
            continue
        for it in ((data.get("items") or {}).get("data") or []):
            codes = set()
            for m in it.get("market") or []:
                if isinstance(m, dict) and m.get("code"):
                    codes.add(str(m["code"]))
            for p in it.get("otherProduct") or []:
                parts = str(p).split(":")
                if len(parts) >= 2 and parts[0] == "TWS":
                    codes.add(parts[1])
            if not it.get("title") or not it.get("newsId"):
                continue
            title = clean_title(html.unescape(str(it["title"])))
            if is_noise(title, "鉅亨網"):
                continue
            ts = it.get("publishAt")
            published = datetime.fromtimestamp(ts, TZ).isoformat(timespec="minutes") if ts else None
            items.append({
                "codes": sorted(c for c in codes if c in tracked),
                "title": title,
                "source": "鉅亨網",
                "url": f"https://news.cnyes.com/news/id/{it['newsId']}",
                "publishedAt": published,
                "via": "cnyes",
                "dateOk": True,
            })
        time.sleep(0.5)
    return items


# ---------------------------------------------------------------- 3. 各家媒體 RSS（發布時間可信）
def fetch_feeds():
    items, ok, failed, seen = [], 0, [], set()
    for src, url in FEEDS:
        try:
            root = ET.fromstring(http_get(url, timeout=25, retries=1))
        except Exception as e:
            failed.append(f"{src}（{str(e)[:40]}）")
            continue
        ok += 1
        for it in root.findall(".//item"):
            title = clean_title(html.unescape(strip_tags(it.findtext("title") or "")).strip())
            link = (it.findtext("link") or "").strip()
            published = parse_any_date(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or "")
            if not title or not link or not published or is_noise(title, src):
                continue
            key = norm_title(title)[:40]
            if key in seen or link in seen:
                continue
            seen.update((key, link))
            items.append({"codes": [], "title": title, "source": src, "url": link,
                          "publishedAt": published, "via": "feed", "dateOk": True})
        time.sleep(0.4)
    return items, ok, failed


# ---------------------------------------------------------------- 4. Google 新聞（逐檔＋逐產業搜尋）
def google_search(query):
    url = GNEWS_URL.format(q=urllib.parse.quote(f"{query} when:{NEWS_KEEP_DAYS}d"))
    root = ET.fromstring(http_get(url, retries=1))
    out = []
    for item in root.findall(".//item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        src_el = item.find("source")
        source = ((src_el.text if src_el is not None else "") or "").strip()
        site = site_of(src_el.get("url") if src_el is not None else "")
        if not title or not link:
            continue
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        title = clean_title(title)
        if is_noise(title, source) or FOREIGN_SITE.search(site) or "香港" in source:
            continue  # 香港媒體的「高力」「大量」等同名公司新聞（例：高力國際＝地產顧問 Colliers）
        out.append({"codes": [], "title": title, "source": source or "Google 新聞", "site": site, "url": link,
                    "publishedAt": parse_any_date(item.findtext("pubDate") or ""), "via": "google"})
    return out


FOREIGN_SITE = re.compile(r"\.hk$|^hk\.|on\.cc$|hket\.com|am730|881903|colliers\.com")


def stock_query(code, name):
    base = QUERY_OVERRIDES.get(code) or short_name(name)
    return " OR ".join([base] + [t for t in GNEWS_EXTRA_TERMS.get(code, []) if t != base])


# ---------------------------------------------------------------- 5. Google 新聞日期驗證
class Blocked(Exception):
    pass


def _fetch(url, data=None, headers=None, timeout=20, limit=None):
    h = dict(UA)
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl(), (resp.read(limit) if limit else resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (429, 403) and "news.google.com" in url:
            raise Blocked(f"{e.code}")
        raise


def google_article_id(link):
    m = re.search(r"/articles/([^?/#]+)", link or "")
    return m.group(1) if m else None


def resolve_google_link(aid):
    """Google 新聞 RSS 的連結是轉址頁，用頁面上的簽章呼叫 batchexecute 取回原始文章網址。"""
    _, raw = _fetch(GN_ARTICLE_URL.format(aid=aid))
    page = raw.decode("utf-8", "replace")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts):
        return None
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{ts.group(1)},"{sg.group(1)}"]')
    body = urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", inner]]])}).encode()
    _, raw = _fetch(GN_BATCH_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    txt = raw.decode("utf-8", "replace")
    try:
        for entry in json.loads(txt.split("\n\n", 1)[1]):
            if isinstance(entry, list) and len(entry) > 2 and entry[0] == "wrb.fr" and entry[1] == "Fbv4je" and entry[2]:
                res = json.loads(entry[2])
                if res and res[0] == "garturlres" and str(res[1]).startswith("http"):
                    return res[1]
    except (IndexError, ValueError, TypeError):
        pass
    m = re.search(r'garturlres\\",\\"(https?:[^"\\]*(?:\\\\u[0-9a-fA-F]{4}[^"\\]*)*)', txt)
    if m:
        return json.loads('"' + m.group(1).replace("\\\\", "\\") + '"')
    return None


ARTICLE_DATE_PATTERNS = [
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:published_time|og:published_time|pubdate|publishdate|publish-date|datePublished|parsely-pub-date|sailthru\.date|article\.published|date)["\'][^>]*content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name|itemprop)=["\'](?:article:published_time|pubdate|publishdate|datePublished|date)["\']',
    r'<time[^>]+datetime=["\']([^"\']+)["\']',
]
LTN_UPLOAD_DATE = re.compile(r"img\.ltn\.com\.tw/Upload/[^\"'\s]*?/(20\d\d)/(\d\d)/(\d\d)/")
URL_DATE = [re.compile(r"/(20\d\d)(\d\d)(\d\d)\d{2,}"), re.compile(r"/(20\d\d)/(\d\d)/(\d\d)/"), re.compile(r"/(20\d\d)-(\d\d)-(\d\d)")]


def article_published(url, now):
    """讀文章頁面的發布時間；找不到就回傳 None。"""
    def plausible(iso):
        return iso and "2015-01-01" <= iso[:10] <= (now + timedelta(days=1)).strftime("%Y-%m-%d")
    for rx in URL_DATE:  # 網址本身帶日期（工商時報 /news/20261023700660）就不用抓頁面
        m = rx.search(url)
        if m:
            iso = parse_any_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
            if plausible(iso) and "ctee.com.tw" in url:
                return iso
    final, raw = _fetch(url, timeout=15, limit=900_000)
    page = raw.decode("utf-8", "replace")
    for pat in ARTICLE_DATE_PATTERNS:
        m = re.search(pat, page, re.I)
        if m:
            iso = parse_any_date(m.group(1))
            if plausible(iso):
                return iso
    m = LTN_UPLOAD_DATE.search(page)
    if m:
        iso = parse_any_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
        if plausible(iso):
            return iso
    for rx in URL_DATE:
        m = rx.search(final or url)
        if m:
            iso = parse_any_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
            if plausible(iso):
                return iso
    return None


class DateChecker:
    """Google 新聞的發布日期驗證＋快取（data/news_dates.json）。"""
    def __init__(self, now):
        self.now = now
        self.now_iso = now.isoformat(timespec="minutes")
        self.left = VERIFY_MAX
        self.blocked = False
        self.stats = {"verified": set(), "stale": set(), "unverifiable": set(), "checked": 0}
        try:
            with open(DATES_PATH, encoding="utf-8") as f:
                self.cache = json.load(f).get("items", {})
        except (FileNotFoundError, json.JSONDecodeError):
            self.cache = {}

    def entry(self, it):
        aid = google_article_id(it.get("url"))
        if not aid:
            return None
        ent = self.cache.get(aid)
        if ent is None:
            ent = self.cache[aid] = {"first": self.now_iso}
        if it.get("site") and not ent.get("site"):
            ent["site"] = it["site"]
        return ent

    def verify(self, it):
        ent = self.entry(it)
        if ent is None or ent.get("date") or ent.get("tries", 0) >= 2:
            return ent
        if ent.get("checked", "")[:10] == self.now_iso[:10] or self.left <= 0 or self.blocked:
            return ent
        self.left -= 1
        self.stats["checked"] += 1
        aid = google_article_id(it.get("url"))
        try:
            real = resolve_google_link(aid)
            if real:
                ent["url"] = real
                ent["site"] = site_of(real) or ent.get("site", "")
                iso = article_published(real, self.now)
                if iso:
                    ent["date"] = iso
        except Blocked as e:
            self.blocked = True
            print(f"::warning::Google 新聞連結解碼被限流（{e}），這次先停止日期驗證")
        except Exception as e:
            ent["err"] = str(e)[:80]
        ent["tries"] = ent.get("tries", 0) + 1
        ent["checked"] = self.now_iso
        time.sleep(VERIFY_PAUSE)
        return ent

    def resolve(self, it, cutoff):
        """回傳 (發布時間, 是否驗證過)；判定為舊文章或日期不可信時回傳 (None, False)。"""
        ent = self.verify(it)
        if ent and ent.get("date"):
            if ent["date"] < cutoff:
                self.stats["stale"].add(it.get("url"))
                return None, False
            self.stats["verified"].add(it.get("url"))
            return ent["date"], True
        self.stats["unverifiable"].add(it.get("url"))
        site = (ent or {}).get("site") or it.get("site") or ""
        if any(site.endswith(s) for s in UNRELIABLE_SITES) or (not site and it.get("source") in UNRELIABLE_SOURCES):
            return None, False
        dates = [d for d in (it.get("publishedAt"), (ent or {}).get("first")) if d]
        published = min(dates) if dates else None  # 舊文章被重新標成新日期時，以第一次看到的時間為準
        if not published or published < cutoff or stale_hint(it.get("title"), published, it.get("codes")):
            return None, False
        return published, False

    def save(self):
        keep_from = (self.now - timedelta(days=DATES_KEEP_DAYS)).isoformat(timespec="minutes")
        items = {k: v for k, v in self.cache.items() if v.get("first", "") >= keep_from}
        with open(DATES_PATH, "w", encoding="utf-8") as f:
            json.dump({"note": "Google 新聞連結對應的原始網址與文章頁面上的發布時間（scripts/fetch_company_news.py 用來排除被重新標上新日期的舊文章；網站不讀這個檔）",
                       "items": items}, f, ensure_ascii=False, separators=(",", ":"))


# 驗證不到日期時的保險：標題內容跟發布時間明顯對不上的（例如10月出現「2月營收」「6/23動土」「去年大賺」、
# 標題寫的股價跟最近一個月的實際成交區間差很多）視為舊文章
PRICE_CLAIM = re.compile(r"(?:衝上|重返|站上|攻上|站回|突破|跌破|失守|守住|收在|來到|回到|跌到|漲到|觸及)\s*(\d{2,5}(?:\.\d+)?)\s*元")
DATED_EVENT = re.compile(r"(?<![\d/.])(1[0-2]|0?[1-9])/([12]\d|3[01]|0?[1-9])(?![\d/])\s*(?:動土|開幕|法說|開工|量產|上市|掛牌|除息|除權|股東會|發表|登場|舉行|啟用|投產|生效|開賣|動工|落成|完工|上路)")
PRICE_RANGE = {}


def load_price_ranges():
    try:
        with open("data/technical.json", encoding="utf-8") as f:
            stocks = json.load(f).get("stocks", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return
    for code, t in stocks.items():
        ser = (t or {}).get("series") or []
        if len(ser) >= 5:
            recent = ser[-25:]
            PRICE_RANGE[code] = (min(x["l"] for x in recent), max(x["h"] for x in recent))


def stale_hint(title, published, codes=None):
    t = title or ""
    try:
        p = datetime.fromisoformat(published)
    except (TypeError, ValueError):
        return None
    for m in re.finditer(r"(?<![\d.])(1[0-2]|[1-9])月(?:合併)?營收", t):
        if (p.month - int(m.group(1))) % 12 > 2:
            return f"{m.group(1)}月營收"
    for m in DATED_EVENT.finditer(t):
        try:
            d = datetime(p.year, int(m.group(1)), int(m.group(2)), tzinfo=TZ)
        except ValueError:
            continue
        if d > p + timedelta(days=200):
            d = d.replace(year=p.year - 1)
        if (p - d).days > 45:
            return f"{m.group(1)}/{m.group(2)}"
    if p.month >= 5 and re.search(r"去年(?:全年)?(?:大賺|獲利|EPS|每股|稅後|賺)", t):
        return "去年獲利"
    if 4 <= p.month <= 11 and re.search(r"過年|春節|農曆年|開工紅包", t):
        return "過年"
    if codes and len(codes) == 1 and codes[0] in PRICE_RANGE:
        lo, hi = PRICE_RANGE[codes[0]]
        for m in PRICE_CLAIM.finditer(t):
            v = float(m.group(1))
            if v > hi * 1.05 or v < lo * 0.95:
                return f"股價{m.group(1)}元"
    return None


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


def load_sectors():
    """產業分類以網站 SEED_STOCKS 的 sector 為準：{產業: [代號...]}"""
    with open(INDEX_HTML, encoding="utf-8") as f:
        text = f.read()
    sectors = {}
    for code, sec in re.findall(r"\{code:'(\w+)'[^{}]*?sector:'([^']+)'", text):
        sectors.setdefault(sec, []).append(code)
    return sectors


def industry_rules(sectors):
    rules = {}
    for sec in sectors:
        terms, pattern = INDUSTRY_KEYWORDS.get(sec) or ([re.sub(r"[／/()（）]", " ", sec).split()[0]], re.escape(sec))
        rules[sec] = (" OR ".join(terms), re.compile(pattern, re.I))
    return rules


# ---------------------------------------------------------------- 合併與輸出
def norm_title(t):
    return re.sub(r"[\s\W_]+", "", t or "").lower()


def pick(pool, checker, cutoff, limit):
    """依時間由新到舊挑出最多 limit 則：Google 新聞先驗證真實發布日期，舊文章與日期不可信的剔除，
    只收重大事件／法說展望／產業趨勢三類，同標題或同連結只留一則。"""
    seen, kept = set(), []
    for it in sorted(pool, key=lambda x: x.get("publishedAt") or "", reverse=True):
        if (it.get("publishedAt") or "") < cutoff:
            continue
        cat = news_category(it.get("title"))
        if not cat:
            continue  # 營收數字、股價漲跌、籌碼目標價這類不收（見 NEWS_RULES）
        key = norm_title(it.get("title"))[:40]
        if not key or key in seen or it.get("url") in seen:
            continue
        out = {k: v for k, v in it.items() if k not in ("codes", "dateOk")}
        if it.get("via") == "google":
            published, ok = checker.resolve(it, cutoff)
            if not published:
                continue
            out["publishedAt"] = published
            if ok:
                out["dateOk"] = True
        elif it.get("via") in ("cnyes", "feed") or it.get("dateOk"):
            out["dateOk"] = True
        seen.add(key)
        seen.add(it.get("url"))
        out["cat"] = cat
        kept.append(out)
        if len(kept) >= limit:
            break
    return sorted(kept, key=lambda x: x.get("publishedAt") or "", reverse=True)


def main():
    now = datetime.now(TZ)
    tracked = load_tracked()
    sectors = load_sectors()
    rules = industry_rules(sectors)
    load_price_ranges()

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

    # 2)～4) 新聞來源
    matchers = build_title_matchers(tracked)
    fresh = []
    try:
        cn = fetch_cnyes(tracked)
        fresh.extend(cn)
        status["cnyes"] = f"ok（{len(cn)}則）"
    except Exception as e:
        status["cnyes"] = f"失敗：{e}"
        print(f"::warning::鉅亨網新聞抓取失敗：{e}")

    feed_items, feed_ok, feed_failed = fetch_feeds()
    fresh.extend(feed_items)
    status["feeds"] = f"成功 {feed_ok}/{len(FEEDS)} 個媒體 RSS、取得 {len(feed_items)} 則" + (f"；失敗：{'、'.join(feed_failed)}" if feed_failed else "")

    g_ok = g_fail = g_items = 0
    g_blocked = False
    google_stock, google_ind = [], []
    queries = [("stock", code, stock_query(code, name)) for code, name in tracked.items()] + \
              [("industry", sec, q) for sec, (q, _) in rules.items()]
    for kind, key, query in queries:
        if g_blocked:
            break
        try:
            got = google_search(query)
            if kind == "stock":
                got = [dict(it, codes=[key]) for it in got if matchers[key].search(it["title"])]
                google_stock.extend(got)
            else:
                got = [dict(it, sectors=[key]) for it in got if rules[key][1].search(it["title"])]
                google_ind.extend(got)
            g_items += len(got)
            g_ok += 1
        except FetchError as e:
            g_fail += 1
            if "429" in str(e) or "403" in str(e):
                g_blocked = True
                print(f"::warning::Google 新聞被限流，這次先停止查詢：{e}")
        except ET.ParseError as e:
            g_fail += 1
            print(f"{key} Google 新聞解析失敗：{e}")
        time.sleep(GNEWS_PAUSE)
    status["google"] = f"成功 {g_ok} 次查詢（{len(tracked)} 檔股名＋{len(rules)} 個產業）、失敗 {g_fail} 次、取得 {g_items} 則" + ("（中途被限流）" if g_blocked else "")
    fresh.extend(google_stock)

    # 標題點名的股票一律標上（鉅亨網的發布者標註也保留）
    tag_codes(fresh, matchers)
    tag_codes(google_ind, matchers)

    checker = DateChecker(now)
    cutoff_news = (now - timedelta(days=NEWS_KEEP_DAYS)).isoformat(timespec="minutes")

    # 個股新聞
    old_news = old.get("news") or {}
    news = {}
    for code in tracked:
        pool = []
        for it in old_news.get(code, []):
            # 舊資料用目前的規則重新檢查：討論區貼文、雜訊、同名誤判（Google 新聞的標題沒提到這檔）一併清掉；
            # 鉅亨網的文章是發布者自己標註的個股，標題沒寫股名也算數
            it = dict(it, title=clean_title(it.get("title")), codes=[code])
            if is_noise(it["title"], it.get("source")):
                continue
            if it.get("via") in ("google", "feed") and not matchers[code].search(it["title"]):
                continue
            pool.append(it)
        pool.extend(it for it in fresh if code in it["codes"])
        kept = pick(pool, checker, cutoff_news, NEWS_PER_STOCK)
        if kept:
            news[code] = kept

    # 產業新聞：Google 產業關鍵字搜尋＋媒體 RSS／鉅亨網標題命中產業關鍵字
    old_ind = old.get("industry") or {}
    industry = {}
    for sec, (_, rx) in rules.items():
        pool = [dict(it, codes=list(it.get("codes") or [])) for it in old_ind.get(sec, []) if rx.search(it.get("title") or "")]
        pool.extend(it for it in google_ind if sec in it.get("sectors", []))
        pool.extend(it for it in fresh if rx.search(it.get("title") or ""))
        kept = pick(pool, checker, cutoff_news, INDUSTRY_PER_SECTOR)
        for it in kept:
            it.pop("sectors", None)
            hit = sorted(c for c, rxs in matchers.items() if rxs.search(it.get("title") or ""))
            if hit:
                it["codes"] = hit
        if kept:
            industry[sec] = kept

    checker.save()
    st = checker.stats
    status["dateCheck"] = (f"Google 新聞發布日期：本次新驗證 {st['checked']} 則；採用文章頁日期 {len(st['verified'])} 則、"
                           f"剔除舊文章 {len(st['stale'])} 則、無法驗證 {len(st['unverifiable'] - st['verified'] - st['stale'])} 則（自由時報股市網等日期不可信的直接不收）"
                           + ("；中途被限流" if checker.blocked else ""))

    output = {
        "generatedAt": now.isoformat(timespec="seconds"),
        "note": "公司重大訊息來自證交所／櫃買中心 OpenAPI（公開資訊觀測站每日重大訊息）；個股與產業新聞來自鉅亨網、各媒體 RSS（科技新報、經濟日報、工商時報、自由財經、中央社、DIGITIMES、Yahoo股市）與 Google 新聞搜尋（72檔股名＋各產業關鍵字），只保存標題與連結作為新聞索引。Google 新聞會把舊文章重新標上新日期，所以逐則讀取文章頁面上的發布時間，舊文章不收。只保留重大事件（event）、法說／展望（outlook）、產業趨勢（trend）三類；重大訊息保留近30天、新聞保留近7天。",
        "sources": status,
        "announcements": announcements,
        "news": news,
        "industry": industry,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"重大訊息 {len(announcements)} 則（本次新抓 {len(anns)} 則）、個股新聞 {sum(len(v) for v in news.values())} 則（{len(news)} 檔有新聞）、"
          f"產業新聞 {sum(len(v) for v in industry.values())} 則（{len(industry)} 個產業）")
    print(json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    main()
