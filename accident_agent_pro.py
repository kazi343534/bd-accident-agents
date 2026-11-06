#!/usr/bin/env python3
"""
Bangladesh Road Accident News Agent - PRO v3 (FAST, no AI key needed)
Dhaka + Chattogram divisions, output in ENGLISH, runs until --rows is filled.
Every location field is taken exactly from the article (nothing guessed).

Install once (inside your accident_env):
    pip install requests trafilatura feedparser googlenewsdecoder deep-translator "selectolax>=0.4.12,<1"

Examples:
    python accident_agent_pro.py --portals --highway --rows 200 --geocode
    python accident_agent_pro.py --portals --rows 2000 --from-date 2020-01-01 --geocode

What is new in v3 (speed):
  * many searches and many article downloads run at the same time (--workers, --search-workers)
  * big time windows that are split automatically only when a search is full (far fewer searches)
  * articles already judged in an earlier run are never downloaded again
  * link decoding has a timeout, so it can no longer hang
  * geocoding runs once at the end instead of slowing every row
  * progress line every 25 articles + a rejected_articles.csv that tells you WHY an article was dropped
Stop any time with Ctrl+C; re-run the same command to continue.
"""
import argparse, csv, hashlib, json, os, re, socket, sys, threading, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, parse_qs, quote_plus, urlunparse

UA = {"User-Agent": "Mozilla/5.0 (research script; road-accident-dataset)"}
BD = timezone(timedelta(hours=6))

CSV_FIELDS = [
    "division", "district", "subdistrict", "police_station", "village_area",
    "road_name", "timestamp", "accident_date", "death_count", "injury_count",
    "accident_type", "vehicle_type",
    "latitude", "longitude", "geocode_level", "geocode_match",
    "subdistrict_original", "police_station_original", "village_area_original", "road_name_original",
    "source_name", "source_url", "article_published",
    "article_title_en", "evidence_en", "article_title_original", "evidence_original",
]

# ---------- optional geocoding (OpenStreetMap Nominatim, free, no key) ----------
GC_PATH = "geocode_cache.json"
GC = json.load(open(GC_PATH, encoding="utf-8")) if os.path.exists(GC_PATH) else {}
_last_geo = [0.0]

def geocode(parts, district):
    """parts = place names from most specific to least (English). Tries the most specific first;
    a hit is accepted only if the OSM address mentions the article's district."""
    import requests
    d_names = [n.lower() for n in DISTRICTS[district][1] if n.isascii()] + [district.lower()]
    if district == "Chattogram": d_names.append("chittagong")
    if district == "Cumilla": d_names.append("comilla")
    parts = [p for p in parts if p]
    for i in range(len(parts)):
        q = ", ".join(parts[i:]) + ", Bangladesh"
        if q not in GC:
            wait = 1.1 - (time.time() - _last_geo[0])
            if wait > 0: time.sleep(wait)
            try:
                r = requests.get("https://nominatim.openstreetmap.org/search",
                                 params={"q": q, "format": "json", "limit": 1, "countrycodes": "bd"},
                                 headers={"User-Agent": "road-accident-research-script/1.0"}, timeout=15)
                data = r.json()
                GC[q] = data[0] if data else None
            except Exception:
                GC[q] = None
            _last_geo[0] = time.time()
        res = GC[q]
        if res and any(n in res.get("display_name", "").lower() for n in d_names):
            return float(res["lat"]), float(res["lon"]), res["display_name"], ("exact_area" if i == 0 else "coarser")
    return None

# ======================= news portals (searched one by one with --portals) =======================
PORTALS = list(dict.fromkeys("""
prothomalo.com kalerkantho.com jugantor.com samakal.com ittefaq.com.bd bd-pratidin.com manobkantha.com.bd
dailyjanakantha.com dainikamadershomoy.com dailyinqilab.com dailynayadiganta.com sangbad.net ajkerpatrika.com
jagonews24.com banglanews24.com bd24live.com dhakapost.com risingbd.com banglatribune.com
bangla.dhakatribune.com bangla.bdnews24.com bangla.thedailystar.net somoynews.tv jamuna.tv channelionline.com
ntvbd.com rtvonline.com ekattor.tv independent24.com kalbela.com mzamin.com bhorerkagoj.com dailysangram.com
dainikazadi.net purbokone.net protidinersangbad.com bonikbarta.net deshrupantor.com alokitobangladesh.com
bangladesherkhabor.net protidinerbangladesh.com sarabangla.net banglamail24.com channel24bd.tv news24bd.tv
dbcnews.tv maasranga.tv dailyamardesh.com arthosuchak.com jaijaidinbd.com dainikshiksha.com
thedailystar.net dhakatribune.com tbsnews.net newagebd.net bdnews24.com observerbd.com daily-sun.com
thefinancialexpress.com.bd theindependentbd.com bssnews.net unb.com.bd newnationbd.com bangladeshpost.net
dailyasianage.com thebangladeshtoday.com en.prothomalo.com bangladeshchronicle.net dhakacourier.com.bd
bd-journal.com
""".split()))

# ======================= places =======================
DISTRICTS = {
    "Dhaka": ("Dhaka", ["ঢাকা", "Dhaka"]),
    "Gazipur": ("Dhaka", ["গাজীপুর", "Gazipur"]),
    "Narayanganj": ("Dhaka", ["নারায়ণগঞ্জ", "Narayanganj"]),
    "Manikganj": ("Dhaka", ["মানিকগঞ্জ", "Manikganj"]),
    "Munshiganj": ("Dhaka", ["মুন্সীগঞ্জ", "মুন্সিগঞ্জ", "Munshiganj"]),
    "Narsingdi": ("Dhaka", ["নরসিংদী", "Narsingdi"]),
    "Kishoreganj": ("Dhaka", ["কিশোরগঞ্জ", "Kishoreganj"]),
    "Tangail": ("Dhaka", ["টাঙ্গাইল", "Tangail"]),
    "Faridpur": ("Dhaka", ["ফরিদপুর", "Faridpur"]),
    "Madaripur": ("Dhaka", ["মাদারীপুর", "Madaripur"]),
    "Shariatpur": ("Dhaka", ["শরীয়তপুর", "শরিয়তপুর", "Shariatpur"]),
    "Rajbari": ("Dhaka", ["রাজবাড়ী", "রাজবাড়ি", "Rajbari"]),
    "Gopalganj": ("Dhaka", ["গোপালগঞ্জ", "Gopalganj"]),
    "Chattogram": ("Chattogram", ["চট্টগ্রাম", "Chattogram", "Chittagong"]),
    "Cox's Bazar": ("Chattogram", ["কক্সবাজার", "Cox's Bazar", "Coxs Bazar"]),
    "Cumilla": ("Chattogram", ["কুমিল্লা", "Cumilla", "Comilla"]),
    "Feni": ("Chattogram", ["ফেনী", "Feni"]),
    "Noakhali": ("Chattogram", ["নোয়াখালী", "Noakhali"]),
    "Lakshmipur": ("Chattogram", ["লক্ষ্মীপুর", "Lakshmipur"]),
    "Chandpur": ("Chattogram", ["চাঁদপুর", "Chandpur"]),
    "Brahmanbaria": ("Chattogram", ["ব্রাহ্মণবাড়িয়া", "Brahmanbaria"]),
    "Rangamati": ("Chattogram", ["রাঙামাটি", "Rangamati"]),
    "Bandarban": ("Chattogram", ["বান্দরবান", "Bandarban"]),
    "Khagrachari": ("Chattogram", ["খাগড়াছড়ি", "Khagrachari"]),
}

# Only DISTRICT names (matched from the article text) are mapped; every smaller place name is
# taken exactly as written in the article and translated, never looked up in a built-in list.
PLACE_EN = {}
for _d, (_dv, _names) in DISTRICTS.items():
    for _n in _names:
        PLACE_EN[_n] = _d


HIGHWAY_RE = re.compile(r"ঢাকা\s*[-–]\s*চট্টগ্রাম|চট্টগ্রাম\s*[-–]\s*ঢাকা|ঢাকা\s*[-–]\s*চিটাগাং|"
                        r"Dhaka\s*[-–]\s*Chattogram|Dhaka\s*[-–]\s*Chittagong|Dhaka\s*[-–]\s*Chattagram|"
                        r"Chattogram\s*[-–]\s*Dhaka|Chittagong\s*[-–]\s*Dhaka|Dhaka\s*[-–]\s*Ctg", re.I)

# ======================= extraction vocab =======================
BN2EN = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
NUMW = {"এক": 1, "একজন": 1, "দুই": 2, "দু": 2, "তিন": 3, "চার": 4, "পাঁচ": 5, "ছয়": 6, "সাত": 7, "আট": 8,
        "নয়": 9, "দশ": 10, "এগারো": 11, "বারো": 12, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
NUM = r"(\d+|এগারো|বারো|দুই|দু|এক|তিন|চার|পাঁচ|ছয়|সাত|আট|নয়|দশ|one|two|three|four|five|six|seven|eight|nine|ten)"
PRE = r"(?<![\u0980-\u09FFA-Za-z0-9])"
QUAL = r"(?:অন্তত|কমপক্ষে|আরও|at least|another)?\s*"
NOTTIME = r"(?!দিন|সপ্তাহ|মাস|বছর|ঘণ্টা|মিনিট|সময়)"

DEATH_NUM = [
    rf"{PRE}{NUM}\s*জন(?:ের)?\s*(?:নিহত|মৃত্যু|মারা|প্রাণ)",
    rf"(?:নিহত(?:\s*হয়েছেন)?|মারা\s*(?:গেছেন|গেছে|যান)|প্রাণ\s*হারিয়েছেন|মৃত্যু\s*হয়েছে)\s*{QUAL}{NUM}\s*জন",
    rf"{PRE}{NUM}\s*(?:people|persons|passengers|others)?\s*(?:were\s*)?(?:killed|dead|died)",
    rf"(?:killed|death of)\s*{NUM}\b",
]
# explicit "one person" wording: "এক পথচারী নিহত", "একজন নিহত", "a pedestrian was killed"
DEATH_ONE = [
    PRE + r"(এক|একজন)\s+" + NOTTIME + r"(?:[\u0980-\u09FF]+\s+){0,3}?(?:নিহত|মারা\s*(?:গেছেন|গেছে|যান)|প্রাণ\s*হারিয়েছেন)",
]
TOLL = [rf"(?:নিহত|মৃতের|মৃত্যুর)\s*সংখ্যা\s*(?:বেড়ে|বেড়ে\s*দাঁড়িয়েছে)\s*{NUM}",
        rf"death toll\s*(?:rose|rises|climbed|reached)\s*(?:to)?\s*{NUM}\b"]
INJ_NUM = [
    rf"{PRE}{NUM}\s*জন(?:ের)?\s*আহত",
    rf"আহত(?:\s*হয়েছেন)?\s*{QUAL}{NUM}\s*জন",
    rf"{PRE}{NUM}\s*(?:others|people|persons)?\s*(?:were\s*)?(?:injured|hurt)",
]
INJ_ONE = [
    PRE + r"(এক|একজন)\s+" + NOTTIME + r"(?:(?!নিহত|মারা)[\u0980-\u09FF]+\s+){0,3}?আহত",
]
VEHICLES = [("কাভার্ড ভ্যান", "covered van"), ("কাভার্ডভ্যান", "covered van"), ("মাইক্রোবাস", "microbus"),
            ("প্রাইভেটকার", "private car"), ("প্রাইভেট কার", "private car"), ("মোটরসাইকেল", "motorcycle"),
            ("সিএনজি", "CNG"), ("অটোরিকশা", "auto-rickshaw"), ("ইজিবাইক", "easy-bike"), ("লেগুনা", "leguna"),
            ("পিকআপ", "pickup"), ("ট্যাংকলরি", "tanker"), ("ট্রাক্টর", "tractor"), ("অ্যাম্বুলেন্স", "ambulance"),
            ("রিকশা", "rickshaw"), ("লরি", "lorry"), ("ট্রাক", "truck"), ("বাস", "bus"), ("কার", "car"),
            ("ভ্যান", "van"), ("motorcycle", "motorcycle"), ("bus", "bus"), ("truck", "truck"),
            ("lorry", "lorry"), ("pickup", "pickup"), ("microbus", "microbus"),
            ("auto-rickshaw", "auto-rickshaw"), ("CNG", "CNG"), ("covered van", "covered van"),
            ("private car", "private car")]
TYPES = [(r"হিট\s*(?:অ্যান্ড|এন্ড|এ্যান্ড)\s*রান|hit[- ]and[- ]run", "hit_and_run"),
         (r"উল্টে|ছিটকে|overturn|capsiz", "overturn"),
         (r"চাপা|চাপ\s*দিয়ে|চাপায়|run over|ran over|crushed", "run_over"),
         (r"সংঘর্ষ|ধাক্কা|collid|collision|hit by|rammed|smashed", "collision")]
MONTHS = {"জানুয়ারি": 1, "ফেব্রুয়ারি": 2, "মার্চ": 3, "এপ্রিল": 4, "মে": 5, "জুন": 6, "জুলাই": 7,
          "আগস্ট": 8, "সেপ্টেম্বর": 9, "অক্টোবর": 10, "নভেম্বর": 11, "ডিসেম্বর": 12}
STOP = {"ওই", "এই", "সেই", "সংশ্লিষ্ট", "স্থানীয়", "পুলিশ", "নিকটস্থ", "একই", "এলাকার", "থানা", "উপজেলা",
        "জেলা", "হাইওয়ে", "মহাসড়ক", "সড়ক", "এ", "ঐ", "যেখানে", "কাছের", "পাশের", "ওই"}
HOSP_AFTER = ("মেডিকেল", "মেডিক্যাল", "বিশ্ববিদ্যালয়", "হাসপাতাল", "Medical", "University")
ROAD_SUFFIX_EN = {"মহাসড়ক": "Highway", "সড়ক": "Road", "বাইপাস": "Bypass", "রোড": "Road",
                  "এক্সপ্রেসওয়ে": "Expressway"}


# ======================= translation (dictionary -> Google Translate -> romanizer) =======================
_CONS = {"ক": "k", "খ": "kh", "গ": "g", "ঘ": "gh", "ঙ": "ng", "চ": "ch", "ছ": "chh", "জ": "j", "ঝ": "jh",
         "ঞ": "n", "ট": "t", "ঠ": "th", "ড": "d", "ঢ": "dh", "ণ": "n", "ত": "t", "থ": "th", "দ": "d",
         "ধ": "dh", "ন": "n", "প": "p", "ফ": "f", "ব": "b", "ভ": "bh", "ম": "m", "য": "j", "র": "r",
         "ল": "l", "শ": "sh", "ষ": "sh", "স": "s", "হ": "h", "ড়": "r", "ঢ়": "rh", "য়": "y", "ৎ": "t"}
_VOW = {"অ": "o", "আ": "a", "ই": "i", "ঈ": "i", "উ": "u", "ঊ": "u", "ঋ": "ri", "এ": "e", "ঐ": "oi",
        "ও": "o", "ঔ": "ou"}
_MATRA = {"া": "a", "ি": "i", "ী": "i", "ু": "u", "ূ": "u", "ৃ": "ri", "ে": "e", "ৈ": "oi", "ো": "o", "ৌ": "ou"}

def romanize(bn):
    """Last-resort Bangla -> Latin so the English columns never contain Bangla script."""
    out, i = [], 0
    while i < len(bn):
        ch = bn[i]
        if ch in "যড঩ঢ" and i + 1 < len(bn) and bn[i + 1] == "়":   # য় ড় ঢ় = base + nukta
            ch = {"য": "য়", "ড": "ড়", "ঢ": "ঢ়"}[ch]; i += 1
        if ch in _CONS:
            out.append(_CONS[ch])
            nxt = bn[i + 1] if i + 1 < len(bn) else ""
            if nxt in _MATRA or nxt == "্":
                pass
            elif nxt and nxt not in "ংঃঁ" and i + 1 < len(bn):
                out.append("a")
        elif ch in _MATRA: out.append(_MATRA[ch])
        elif ch in _VOW: out.append(_VOW[ch])
        elif ch == "ং": out.append("ng")
        elif ch == "ঃ": out.append("h")
        elif ch in "্ঁ": pass
        elif ord(ch) < 128: out.append(ch)
        i += 1
    return "".join(out)

TC_PATH = "translation_cache.json"
TC = json.load(open(TC_PATH, encoding="utf-8")) if os.path.exists(TC_PATH) else {}
_tl = threading.local()
_GT_WARNED = [False]
TRANS_SEM = threading.Semaphore(2)      # Google Translate is polite-limited: max 2 at once
BANGLA = re.compile(r"[\u0980-\u09FF]")

def get_gt():
    if getattr(_tl, "gt", None) is None:
        try:
            from deep_translator import GoogleTranslator
            _tl.gt = GoogleTranslator(source="auto", target="en")
        except Exception:
            _tl.gt = False
            if not _GT_WARNED[0]:
                _GT_WARNED[0] = True
                print("  (deep-translator not installed: titles/evidence will be romanized or blank. "
                      "Run: pip install deep-translator)", flush=True)
    return _tl.gt or None

FIX_PATH = "place_names_fix.csv"     # YOU can correct English names here; they override everything
FIXES = {}

def load_fixes():
    FIXES.clear()
    if os.path.exists(FIX_PATH):
        with open(FIX_PATH, encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and BANGLA.search(row[0]) and row[1].strip():
                    FIXES[row[0].strip()] = row[1].strip()

def translate_place(w):
    if not w:
        return None
    if w in FIXES:
        return FIXES[w]
    if not BANGLA.search(w):
        return w
    if w in PLACE_EN:
        return PLACE_EN[w]
    if w in TC:
        return TC[w]
    en = None
    gt = get_gt()
    if gt:
        try:
            with TRANS_SEM:
                en = gt.translate(w); time.sleep(0.15)
        except Exception:
            en = None
    # Google sometimes returns the MEANING of a name ("head", "market", all lowercase) instead of its sound;
    # a real place name comes back capitalised. Lowercase = meaning -> use the phonetic romanization instead.
    if not en or BANGLA.search(en) or len(en) > 40 or en.islower():
        en = romanize(w).title()
    TC[w] = en
    return en

def translate_text(t):
    if not t:
        return ""
    if not BANGLA.search(t):
        return t
    if t in TC:
        return TC[t]
    gt = get_gt()
    if not gt:
        return ""
    for _ in range(2):
        try:
            with TRANS_SEM:
                en = gt.translate(t[:4500]); time.sleep(0.15)
            if en and not BANGLA.search(en):
                TC[t] = en
                return en
        except Exception:
            time.sleep(2)
    return ""

def translate_road(r):
    if not r:
        return None
    if not BANGLA.search(r):
        return r
    m = re.match(r"(.*?)\s*(মহাসড়ক|আঞ্চলিক\s*সড়ক|এক্সপ্রেসওয়ে|সড়ক|বাইপাস|রোড)\S*$", r.strip())
    if not m:
        return translate_place(r)
    head, suf = m.group(1).strip(), re.sub(r"\s+", " ", m.group(2))
    parts = [translate_place(p) for p in re.split(r"\s*[-–]\s*", head) if p]
    suf_en = "Regional Road" if suf.startswith("আঞ্চলিক") else ROAD_SUFFIX_EN.get(suf, suf)
    return ("-".join(p for p in parts if p) + " " + suf_en).strip()


# ======================= helpers =======================
def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def conv(s):
    return s.translate(BN2EN)

def canon_url(u):
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), "", "", ""))

def title_key(t):
    return re.sub(r"[^\w]+", "", (t or "").lower())

def parse_date(s):
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[।\.\?!])\s+|\n", text) if s.strip()]

def to_int(tok):
    tok = tok.lower()
    return int(tok) if tok.isdigit() else NUMW.get(tok)

def first_count(pats, sents):
    for s in sents:
        c = conv(s)
        for p in pats:
            m = re.search(p, c, re.I)
            if m and to_int(m.group(1)) is not None:
                return to_int(m.group(1)), s
    return None, None

def find_district(title, sents):
    best = None
    for text in [title] + sents:
        for dist, (div, names) in DISTRICTS.items():
            for n in names:
                for m in re.finditer(re.escape(n), text):
                    before = text[m.start() - 1] if m.start() else " "
                    after = text[m.end():m.end() + 1]
                    rest = text[m.end():m.end() + 12].lstrip()
                    if before in "-–" or after in "-–" or rest.startswith(HOSP_AFTER):
                        continue
                    if best is None or m.start() < best[0]:
                        best = (m.start(), dist, div)
        if best:
            return best[1], best[2]
    return None, None

BAD_NAMES = {"নামক", "নামের", "নামে", "মাথা", "আলী", "চট্ট", "চৌদ্দ", "রবিবার", "সোমবার", "মঙ্গলবার", "বুধবার",
             "বৃহস্পতিবার", "শুক্রবার", "শনিবার", "সকাল", "সকালে", "রাত", "রাতে", "ভোর", "দুপুর", "বিকেল",
             "সন্ধ্যা", "গতকাল", "আজ", "মডেল", "ট্রাফিক", "ওয়ার্ড", "নং", "কাছে", "পাশে", "সামনে", "পাশের"}
_DISTRICT_WORDS = [n for v in DISTRICTS.values() for n in v[1] if BANGLA.search(n)]

def valid_place(w):
    """A place name must be whole words (not a cut-off piece of a longer word, not a common word)."""
    w = norm(w)
    if len(w) < 3 or w == "সদর":
        return False
    if any(t in STOP or t in BAD_NAMES for t in w.split()):
        return False
    if any(len(w) >= 3 and n != w and n.startswith(w) for n in _DISTRICT_WORDS):   # e.g. চট্ট of চট্টগ্রাম
        return False
    return True

def word_before(pattern, sents):
    for s in sents:
        for m in re.finditer(pattern, s):
            w = norm(m.group(1))
            if valid_place(w):
                return w
    return None

def to_time(sents):
    for s in sents[:5]:
        c = conv(s)
        m = re.search(r"(ভোর|সকাল|দুপুর|বিকেল|বিকাল|সন্ধ্যা|রাত)\s*(\d{1,2})\s*টা(?:\s*(\d{1,2})\s*মিনিট)?", c)
        if m:
            per, h, mi = m.group(1), int(m.group(2)) % 12 or 12, int(m.group(3) or 0)
            if per == "দুপুর":
                h = 12 if h == 12 else (h + 12 if h < 6 else h)
            elif per in ("বিকেল", "বিকাল", "সন্ধ্যা"):
                h = h + 12 if h < 12 else h
            elif per == "রাত":
                h = 0 if h == 12 else (h + 12 if h >= 6 else h)
            return f"{h:02d}:{mi:02d}"
        m = re.search(r"\b(\d{1,2}):(\d{2})\b", c)
        if m and int(m.group(1)) < 24:
            return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None

def find_date(lead_text, pub_date):
    c = conv(lead_text)
    if re.search(r"গতকাল|গত\s*রাত|yesterday|last night", c, re.I):
        return pub_date - timedelta(days=1)
    if re.search(r"\bআজ\b|today|this morning", c, re.I):
        return pub_date
    m = re.search(r"(\d{1,2})\s*(" + "|".join(MONTHS) + r")(?:\s*(\d{4}))?", c)
    if m:
        try:
            d = datetime(int(m.group(3) or pub_date.year), MONTHS[m.group(2)], int(m.group(1))).date()
            if d > pub_date + timedelta(days=1):
                d = d.replace(year=d.year - 1)
            return d
        except ValueError:
            pass
    return None


AREA_RE = (r"(?<![\u0980-\u09FF])([\u0980-\u09FF]+)\s+(?:নামক\s+)?(?:এলাকায়|এলাকার|এলাকা|মোড়ে|মোড়|বাজারে|বাজার|গ্রামে|বাইপাস|ব্রিজ|সেতু|"
           r"ফ্লাইওভার|বাসস্ট্যান্ড|বাস\s*স্ট্যান্ড|টোল\s*প্লাজা|ইউনিয়নে|ইউনিয়নের|পয়েন্টে|চৌরাস্তা|চৌরাস্তায়|"
           r"স্টেশনে|স্টেশন|ক্যান্টনমেন্ট|বিসিক)")


def extract_rules(title, text, pub_date, highway_only=False, why=None):
    """0 or 1 record. Anything not explicitly written stays None. why[0] = reason when nothing is returned."""
    def no(reason):
        if why is not None: why.append(reason)
        return []
    title = title or ""
    if re.search(r"মাসে|বছরে|সপ্তাহে|ঈদযাত্রা|ঝরল|ঝরেছে|সারা\s*দেশে|সারাদেশে|nationwide|last month|this year", title):
        return no("summary/statistics article")
    sents = sentences(text)
    lead = sents[:6]
    lead_text = title + " " + " ".join(lead[:4])

    if not re.search(r"দুর্ঘটনা|ধাক্কা|চাপা|সংঘর্ষ|উল্টে|ছিটকে|পিষ্ট|accident|crash|collid|run over|rammed|overturn|hit by",
                     lead_text, re.I):
        return no("not an accident report")
    vehicles = []
    for bn, en in VEHICLES:
        if bn.lower() in lead_text.lower() and en not in vehicles:
            vehicles.append(en)
    if re.search(r"ট্রেন|রেল|train", lead_text, re.I) and not vehicles:
        return no("train/rail accident")

    hw = HIGHWAY_RE.search(title + " " + " ".join(lead))
    if highway_only and not hw:
        return no("not about Dhaka-Chattogram highway")

    district, division = find_district(title, lead[:4])
    if not district:
        return no("no district named")   # not named in the article -> not recorded (no guessing)

    # deaths: numbers in body -> final toll -> explicit "one" -> headline
    deaths, d_ev = first_count(DEATH_NUM, lead)
    toll, t_ev = first_count(TOLL, sents[:10])
    if toll is not None:
        deaths, d_ev = toll, t_ev
    if deaths is None:
        deaths, d_ev = first_count(DEATH_ONE, lead)
    injured, i_ev = first_count(INJ_NUM, lead)
    if injured is None:
        injured, i_ev = first_count(INJ_ONE, lead)
    if deaths is None:
        deaths, d_ev = first_count(DEATH_NUM + DEATH_ONE, [title])
    if injured is None:
        injured, i_ev = first_count(INJ_NUM + INJ_ONE, [title])
    if deaths is None and injured is None:
        return no("no death/injury count")
    evidence = next((e for e in (d_ev, i_ev) if e and e != title), d_ev or i_ev)
    if not (evidence in text or evidence == title):
        return no("evidence not found in text")

    atype = "other"
    for pat, label in TYPES:
        if re.search(pat, lead_text, re.I):
            atype = label; break

    road = None
    joined = " ".join(lead)
    m = re.search(r"([\u0980-\u09FF]+(?:\s*[-–]\s*[\u0980-\u09FF]+)+)\s*(মহাসড়ক|আঞ্চলিক\s*সড়ক|এক্সপ্রেসওয়ে|সড়ক|বাইপাস|রোড)", joined)
    if m:
        road = norm(m.group(1) + " " + m.group(2))
    else:
        m = re.search(r"([\u0980-\u09FF]+)\s*(মহাসড়ক|এক্সপ্রেসওয়ে|বাইপাস)", joined)
        if m and m.group(1) not in STOP:
            road = norm(m.group(1) + " " + m.group(2))
        else:
            m = re.search(r"([A-Z][\w]+(?:-[A-Z][\w]+)+)\s*(?:Highway|Road|highway|Expressway)", joined)
            if m:
                road = m.group(0)
            elif "এক্সপ্রেসওয়ে" in lead_text:
                road = "এক্সপ্রেসওয়ে"

    adate = find_date(lead_text, pub_date)
    return [{
        "division": division, "district": district,
        "subdistrict": word_before(r"(?<![\u0980-\u09FF])([\u0980-\u09FF]+(?:\s+সদর)?)\s+উপজেলা", lead),
        "police_station": word_before(r"(?<![\u0980-\u09FF])([\u0980-\u09FF]+(?:\s+সদর)?)(?:\s+(?:মডেল|হাইওয়ে|ট্রাফিক))?\s+থানা", sents[:10]),
        "village_area": word_before(AREA_RE, sents[:8]),
        "road_name": road, "timestamp": to_time(lead),
        "accident_date": adate.isoformat() if adate else None,
        "death_count": deaths, "injury_count": injured, "accident_type": atype,
        "vehicle_type": ", ".join(vehicles) or None, "evidence": evidence,
    }]


# ======================= discovery =======================
RULES_VERSION = 4          # bump when extraction rules change, so old "rejected" verdicts are re-checked
RL = {"until": 0.0}        # shared "everybody slow down" timer (used after a 429)
DECODE_SEM = threading.Semaphore(3)   # Google link decoding: max 4 at once
FULL_PAGE = 40             # Google News cuts results off well below 100 in practice; >= this means "window is full, split it"
MIN_WINDOW_DAYS = 7
SEARCH_DELAY = 1.8         # seconds each search worker rests after a search (gentler on Google = fewer blocks)
CANARY = {"label": "canary", "gnews": True, "tries": 0,   # a search that always has results; tells "blocked" from "no news"
          "url": "https://news.google.com/rss/search?q=" + quote_plus("সড়ক দুর্ঘটনা নিহত") + "&hl=bn&gl=BD&ceid=BD:bn"}

UA_BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
              "Accept-Language": "bn,en;q=0.8"}
USE_BROWSER_UA = [False]
BLOCK = {"n403": {}, "ok": set(), "dead": set()}   # per-site circuit breaker for HTTP 403

def host_key(url):
    return urlparse(url).netloc.lower().removeprefix("www.")

def google_check():
    """Ask Google News one easy question before starting. Returns (blocked, http_code_or_error)."""
    try:
        r = sess().get(CANARY["url"], timeout=(6, 15))
    except Exception as e:
        return True, type(e).__name__
    head = r.content[:600].lower()
    blocked = r.status_code in (429, 503, 403) or (r.status_code == 200 and b"<rss" not in head and b"<?xml" not in head)
    return blocked, r.status_code

def part_key(qd):
    """Identifies one kind of search (portal + part, or a general query) so a broken one can be skipped."""
    if qd.get("portal"):
        return (qd["portal"], qd.get("part"))
    return ("gen", (qd.get("gen") or (qd.get("label", "?"),))[0])

def portal_dead(portal):
    return bool(portal) and any(h == portal or h.endswith("." + portal) for h in BLOCK["dead"])

ERRS = {}
def note_err(kind, msg):
    """Count errors by cause and print the first few distinct ones, so a failure is never silent."""
    key = (kind, str(msg)[:100]); ERRS[key] = ERRS.get(key, 0) + 1
    if ERRS[key] == 1 and len(ERRS) <= 8:
        print(f"  ! {kind} problem: {str(msg)[:140]}", flush=True)

def sess():
    s = getattr(_tl, "sess", None)
    if s is None:
        import requests
        s = requests.Session(); s.headers.update(UA_BROWSER if USE_BROWSER_UA[0] else UA)
        _tl.sess = s
    return s

def month_windows(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        yield f"{y}-{m:02d}-01", f"{ny}-{nm:02d}-01"
        y, m = ny, nm

def bing_q(label, q):
    return {"label": label, "gnews": False, "tries": 0,
            "url": f"https://www.bing.com/news/search?q={quote_plus(q)}&format=rss&setlang=bn&cc=BD"}

def google_q(label, q, a_, b_, hl, ceid, portal=None):
    full = f"after:{a_} before:{b_} {q}"        # dates first: Google ignores everything after ~32 words
    return {"label": label, "gnews": True, "portal": portal, "tries": 0,
            "url": f"https://news.google.com/rss/search?q={quote_plus(full)}&hl={hl}&gl=BD&ceid={ceid}"}

HW_PHRASES = '("ঢাকা-চট্টগ্রাম মহাসড়ক" OR "ঢাকা-চট্টগ্রাম মহাসড়কে" OR "ঢাকা-চট্টগ্রাম হাইওয়ে" OR "চট্টগ্রাম-ঢাকা মহাসড়ক")'
HW_ACC = "(দুর্ঘটনা OR নিহত)"      # short on purpose: Google only reads the first ~32 words of a search
HW_EN = '("Dhaka-Chattogram highway" OR "Dhaka-Chittagong highway" OR "Dhaka-Ctg highway")'
HW_EN_ACC = "(accident OR killed OR crash)"
ACC_WORDS = "(দুর্ঘটনা OR নিহত OR সংঘর্ষ OR ধাক্কা OR আহত OR উল্টে OR accident OR killed OR crash OR injured OR dead)"
DHAKA_DIV = "(ঢাকা OR Dhaka OR গাজীপুর OR নারায়ণগঞ্জ OR সাভার OR টাঙ্গাইল OR মানিকগঞ্জ)"
CTG_DIV = "(চট্টগ্রাম OR Chattogram OR কুমিল্লা OR কক্সবাজার OR ফেনী OR নোয়াখালী)"
ALL_BASE = '("সড়ক দুর্ঘটনা" OR "road accident" OR "road crash" OR সড়কে নিহত)'
# --deep: every district gets its own search so no district is crowded out by Dhaka/Chattogram
_DB = [(v[1][0], v[1][-1]) for v in DISTRICTS.values()]
DIST_GROUPS = [" OR ".join(x for pair in _DB[i:i + 3] for x in pair) for i in range(0, len(_DB), 3)]

def portal_query(portal, a, b, highway, part):
    """One Google News search for one portal, one date window [a, b) and one part."""
    A, B = a.isoformat(), b.isoformat()
    if part == "hw":                       # exact highway phrases (all spellings) + accident words
        q = f"site:{portal} {HW_PHRASES} {HW_ACC}"
    elif part == "hw2":                    # loose Bangla wording: matches other word forms of the same words
        q = f"site:{portal} ঢাকা চট্টগ্রাম মহাসড়ক (নিহত OR দুর্ঘটনা OR আহত OR সংঘর্ষ)"
    elif part == "hw4":                    # exact English phrases
        q = f"site:{portal} {HW_EN} {HW_EN_ACC}"
    elif part == "hw3":                    # loose English wording
        q = f"site:{portal} Dhaka Chattogram OR Chittagong highway (accident OR killed OR crash OR injured)"
    elif part == "dhaka":
        q = f"site:{portal} {ALL_BASE} {DHAKA_DIV}"
    elif part == "ctg":
        q = f"site:{portal} {ALL_BASE} {CTG_DIV}"
    else:                                  # "g0", "g1", ... district groups (--deep)
        q = f"site:{portal} {ALL_BASE} ({DIST_GROUPS[int(part[1:])]})"
    qd = google_q(f"{portal} {A}..{B} {part}", q, A, B, "bn", "BD:bn", portal)
    qd.update(a=a, b=b, part=part, highway=highway, splittable=True)
    return qd

def general_query(label, q, a, b, hl, ceid):
    """A search on all sites (not one portal); splittable like the portal searches."""
    qd = google_q(f"{label} {a.isoformat()}..{b.isoformat()}", q, a.isoformat(), b.isoformat(), hl, ceid)
    qd.update(a=a, b=b, splittable=True, gen=(label, q, hl, ceid))
    return qd

def split_query(qd):
    """A full search is split in two halves (newer half first) so nothing is lost to the result cap."""
    mid = qd["a"] + (qd["b"] - qd["a"]) // 2
    if qd.get("gen"):
        lab, q, hl, ceid = qd["gen"]
        return [general_query(lab, q, mid, qd["b"], hl, ceid), general_query(lab, q, qd["a"], mid, hl, ceid)]
    return [portal_query(qd["portal"], mid, qd["b"], qd["highway"], qd["part"]),
            portal_query(qd["portal"], qd["a"], mid, qd["highway"], qd["part"])]

def day_windows(start, end, days):
    """(a, b) date windows, newest first, b exclusive."""
    b, s = end.date() + timedelta(days=1), start.date()
    while b > s:
        a = max(s, b - timedelta(days=days))
        yield a, b
        b = a

def general_queue(highway, start, end):
    """Searches on ALL websites (not only the portal list), 30-day windows, newest first, split when full."""
    q = deque()
    wins = list(day_windows(start, end, 30))
    recent = (datetime.now(timezone.utc) - end).days < 60      # Bing only returns recent news
    if highway:
        for t in (["ঢাকা-চট্টগ্রাম মহাসড়কে দুর্ঘটনা", "ঢাকা-চট্টগ্রাম মহাসড়কে নিহত",
                   "Dhaka-Chattogram highway accident", "Dhaka-Chattogram highway crash killed"] if recent else []):
            q.append(bing_q(t, t))
        for a_, b_ in wins:
            for t, hl, ceid in [('"ঢাকা-চট্টগ্রাম মহাসড়ক" দুর্ঘটনা', "bn", "BD:bn"),
                                ('"ঢাকা-চট্টগ্রাম মহাসড়কে" নিহত', "bn", "BD:bn"),
                                ('ঢাকা চট্টগ্রাম মহাসড়ক নিহত আহত', "bn", "BD:bn"),
                                ('"ঢাকা-চট্টগ্রাম হাইওয়ে" দুর্ঘটনা', "bn", "BD:bn"),
                                ('"চট্টগ্রাম-ঢাকা মহাসড়ক" দুর্ঘটনা', "bn", "BD:bn"),
                                ('"Dhaka-Chattogram highway" accident', "en-BD", "BD:en"),
                                ('"Dhaka-Chittagong highway" accident killed', "en-BD", "BD:en"),
                                ('Dhaka Chattogram highway road crash dead injured', "en-BD", "BD:en")]:
                q.append(general_query(t, t, a_, b_, hl, ceid))
    else:
        for d in ([v[1][0] for v in DISTRICTS.values()] if recent else []):
            t = f"{d} সড়ক দুর্ঘটনা নিহত"; q.append(bing_q(t, t))
        for p in (["Dhaka", "Chattogram", "Gazipur", "Narayanganj", "Cox's Bazar", "Cumilla"] if recent else []):
            t = f"{p} road accident killed"; q.append(bing_q(t, t))
        for a_, b_ in wins:
            for d in [v[1][0] for v in DISTRICTS.values()]:
                t = f"{d} সড়ক দুর্ঘটনা নিহত"
                q.append(general_query(t, t, a_, b_, "bn", "BD:bn"))
                t2 = f"{d} বাস ট্রাক মোটরসাইকেল দুর্ঘটনা আহত"
                q.append(general_query(t2, t2, a_, b_, "bn", "BD:bn"))
            for p in ["Dhaka", "Chattogram", "Gazipur", "Narayanganj", "Cox's Bazar", "Cumilla"]:
                t = f"{p} road accident killed"
                q.append(general_query(t, t, a_, b_, "en-BD", "BD:en"))
    return q

def build_queue(highway, start, end, portals, by, window_days, deep=False, also_general=False):
    q = deque()
    if portals:
        if highway:
            parts = ["hw", "hw2", "hw3", "hw4"]
        else:
            parts = ["dhaka", "ctg"] + ([f"g{i}" for i in range(len(DIST_GROUPS))] if deep else [])
        wins = list(day_windows(start, end, window_days))
        if by == "month":                     # rotate through all portals inside each window
            for a, b in wins:
                for pt in portals:
                    for p in parts:
                        q.append(portal_query(pt, a, b, highway, p))
        else:                                 # finish one portal, then the next
            for pt in portals:
                for a, b in wins:
                    for p in parts:
                        q.append(portal_query(pt, a, b, highway, p))
        if also_general:
            q.extend(general_queue(highway, start, end))
        return q
    return general_queue(highway, start, end)

def do_search(qd):
    """Runs in a search thread. Returns (raw_count, entries) or None if the search itself failed."""
    import feedparser
    for _ in range(2):
        w = RL["until"] - time.time()
        if w > 0:
            time.sleep(w)
        try:
            resp = sess().get(qd["url"], timeout=(6, 15))
            head = resp.content[:600].lower()
            soft = qd["gnews"] and resp.status_code == 200 and b"<rss" not in head and b"<?xml" not in head
            if resp.status_code in (429, 503, 403) or soft:
                if RL["until"] < time.time():            # first search to notice this block
                    RL["strikes"] = RL.get("strikes", 0) + 1
                    wait = min(90 * 2 ** (RL["strikes"] - 1), 1800)
                    print(f"  Google answered {'with a block page' if soft else resp.status_code}: pausing ALL searches "
                          f"{wait / 60:g} min (block {RL['strikes']}/5, the agent stops after 5)", flush=True)
                    RL["until"] = time.time() + wait
                continue
            entries = feedparser.parse(resp.content).entries
            RL["strikes"] = 0
            break
        except Exception:
            time.sleep(2)
    else:
        return None
    out = []
    for e in entries:
        link = e.get("link", "")
        real = link if qd["gnews"] else parse_qs(urlparse(link).query).get("url", [link])[0]
        if not real.startswith("http") or "bing.com" in urlparse(real).netloc:
            continue
        out.append({"url": real, "canon": real if qd["gnews"] else canon_url(real),
                    "title": norm(e.get("title")), "published": parse_date(e.get("published", "")),
                    "gnews": qd["gnews"], "portal": qd.get("portal")})
    time.sleep(SEARCH_DELAY)
    return len(entries), out

def resolve_gnews(url, cache):
    if cache.get(url):
        return cache[url]
    from googlenewsdecoder import gnewsdecoder
    real = None
    with DECODE_SEM:
        for attempt in range(2):
            try:
                try:
                    r = gnewsdecoder(url, interval=1)
                except TypeError:
                    r = gnewsdecoder(url)
                if (r.get("success") or r.get("status")) and r.get("decoded_url"):   # key name differs by version
                    real = r["decoded_url"]; break
                note_err("decode", r.get("message") or r)
            except Exception as e:
                note_err("decode", f"{type(e).__name__}: {e}")
            time.sleep(2 * (attempt + 1))
    if real:
        cache[url] = real          # failures are NOT cached, so a later run can retry them
    return real

def fetch_article(url):
    import trafilatura
    h = host_key(url)
    if h in BLOCK["dead"]:
        return None
    r = sess().get(url, timeout=(6, 15), allow_redirects=True)
    if r.status_code == 200:
        BLOCK["ok"].add(h)
    else:
        note_err("download", f"HTTP {r.status_code} from {urlparse(url).netloc}")
        if r.status_code == 403:
            BLOCK["n403"][h] = BLOCK["n403"].get(h, 0) + 1
            if BLOCK["n403"][h] >= 5 and h not in BLOCK["ok"] and h not in BLOCK["dead"]:
                BLOCK["dead"].add(h)
                print(f"  ! {h} refuses downloads (HTTP 403) - skipping this portal"
                      f"{'' if USE_BROWSER_UA[0] else '. Try adding --browser-ua'}", flush=True)
        return None
    text = trafilatura.extract(r.text, include_comments=False, favor_recall=True)
    if not text or len(text) < 200:
        note_err("download", f"no article text found on {urlparse(url).netloc}")
        return None
    meta = trafilatura.extract_metadata(r.text)
    return {"final_url": r.url, "text": text,
            "title": norm(meta.title) if meta and meta.title else ""}


# ======================= dedupe =======================
def ev_key(r):
    return "|".join(str(r.get(k) or "").lower().strip()
                    for k in ("accident_date", "district", "death_count", "injury_count"))

def area_set(r):
    return {norm(r.get(k)).lower() for k in ("subdistrict_original", "police_station_original", "village_area_original")
            if norm(r.get(k))}

def is_dup_event(r, events):
    """Same date + district + deaths + injuries AND (same area, or one of them has no area) = same accident."""
    a = area_set(r)
    return any(not a or not p or (a & p) for p in events.get(ev_key(r), []))

def similar_title(t, titles):
    if not t:
        return False
    n = len(t)
    for tt in titles:
        if abs(len(tt) - n) > 0.2 * n:
            continue
        sm = SequenceMatcher(None, t, tt)
        if sm.real_quick_ratio() > 0.9 and sm.quick_ratio() > 0.9 and sm.ratio() > 0.9:
            return True
    return False

def load_existing(path):
    urls, events, titles, n = set(), {}, set(), 0
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                n += 1
                urls.add(canon_url(r["source_url"]))
                events.setdefault(ev_key(r), []).append(area_set(r))
                titles.add(title_key(r.get("article_title_original") or r.get("article_title_en")))
    return urls, events, titles, n


# ======================= place-name quality passes =======================
AREA_FIELDS = ("subdistrict", "police_station", "village_area")

def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def write_rows(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore", restval="")
        wr.writeheader(); wr.writerows(rows)
    os.replace(tmp, path)

def clean_existing(path):
    """Rows saved by an older version with a broken place name (cut-off word like a half 'Chattogram') are moved
    out to rows_removed_bad_place.csv. Their articles are judged again with the fixed rules and saved correctly."""
    if not os.path.exists(path):
        return
    rows = read_rows(path)
    keep, bad = [], []
    for r in rows:
        if any((r.get(f + "_original") or "") and not valid_place(r[f + "_original"]) for f in AREA_FIELDS):
            bad.append(r)
        else:
            keep.append(r)
    if not bad:
        return
    new = not os.path.exists("rows_removed_bad_place.csv")
    with open("rows_removed_bad_place.csv", "a", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore", restval="")
        if new:
            wr.writeheader()
        wr.writerows(bad)
    write_rows(path, keep)
    print(f"Cleanup: {len(bad)} old rows had a wrong place name; they were moved to rows_removed_bad_place.csv "
          f"and will be re-read with the fixed rules.", flush=True)

def finalize_places(path):
    """1) apply your corrections from place_names_fix.csv to every row; 2) add every new place name to that file
    so you can correct its English once and have all rows updated."""
    if not os.path.exists(path):
        return
    rows = read_rows(path)
    changed, found = False, {}
    for r in rows:
        for f in AREA_FIELDS:
            o = (r.get(f + "_original") or "").strip()
            if not o:
                continue
            if o in FIXES:
                if r.get(f) != FIXES[o]:
                    r[f] = FIXES[o]; changed = True
                    r["latitude"] = r["longitude"] = r["geocode_match"] = r["geocode_level"] = ""   # re-geocode
            elif r.get(f):
                found[o] = r[f]
    if changed:
        write_rows(path, rows)
        print("Applied your corrections from place_names_fix.csv to the CSV.", flush=True)
    if found:
        new = not os.path.exists(FIX_PATH)
        with open(FIX_PATH, "a", newline="", encoding="utf-8-sig") as f:
            wr = csv.writer(f)
            if new:
                wr.writerow(["bangla", "english"])
            for o in sorted(found):
                wr.writerow([o, found[o]])
                FIXES[o] = found[o]
        print(f"{len(found)} new place names were added to {FIX_PATH}. Open it, correct any English name that is "
              f"wrong, save, and run the agent again (even with --rows already reached): every row is updated.",
              flush=True)


# ======================= geocoding pass (runs once, at the end) =======================
def geocode_pass(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    todo = [r for r in rows if not r.get("latitude") and r.get("district") in DISTRICTS]
    if not todo:
        return
    print(f"\nAdding latitude/longitude to {len(todo)} rows (about 1 per second, cached ones are instant). "
          f"Press Ctrl+C once to skip - the rows stay saved.", flush=True)
    hit = 0
    try:
        for i, r in enumerate(todo, 1):
            g = geocode([r.get("village_area"), r.get("subdistrict") or r.get("police_station"), r["district"]],
                        r["district"])
            if g:
                r["latitude"], r["longitude"], r["geocode_match"], r["geocode_level"] = g[0], g[1], g[2], g[3]
                hit += 1
            if i % 10 == 0:
                print(f"   geocoded {i}/{len(todo)} ({hit} found)", flush=True)
    except KeyboardInterrupt:
        print("  geocoding skipped (rows are kept; run again with --geocode to finish).", flush=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore", restval="")
        wr.writeheader(); wr.writerows(rows)
    os.replace(tmp, path)
    json.dump(GC, open(GC_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Geocoding done: {hit}/{len(todo)} rows got coordinates.", flush=True)


# ======================= preflight =======================
def preflight():
    missing = []
    for mod in ("requests", "feedparser", "trafilatura"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if missing:
        sys.exit(f"Missing libraries: {', '.join(missing)}\nRun: pip install {' '.join(missing)}")
    try:
        from googlenewsdecoder import gnewsdecoder  # noqa: F401
    except Exception as e:
        sys.exit("googlenewsdecoder cannot start: " + str(e)[:200] +
                 '\nFix:  pip install --upgrade googlenewsdecoder "selectolax>=0.4.12,<1"')


# ======================= main =======================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=0, help="stop when the CSV has this many rows (0 = run through everything)")
    ap.add_argument("--highway", action="store_true", help="only the Dhaka-Chattogram highway")
    ap.add_argument("--from-date", default="2018-01-01", help="oldest date to search back to")
    ap.add_argument("--to-date", default="", help="newest date (default today)")
    ap.add_argument("--portals", action="store_true", help="search every news portal (see --list-portals)")
    ap.add_argument("--portals-file", default="", help="text file, one extra portal domain per line (added to the list)")
    ap.add_argument("--by", choices=["portal", "month"], default="month",
                    help="month = rotate through all portals inside each time window (default, spreads rows evenly); "
                         "portal = finish one portal then the next")
    ap.add_argument("--window-days", type=int, default=0,
                    help="first search window per portal in days (default: 180 for --highway, 90 otherwise). "
                         "A window that returns a full page is split automatically.")
    ap.add_argument("--deep", action="store_true",
                    help="all-roads mode: also run one search per group of districts (finds far more, takes longer)")
    ap.add_argument("--also-general", action="store_true",
                    help="after the portal searches, also search ALL websites (not only the portal list), month by month")
    ap.add_argument("--workers", type=int, default=8, help="articles downloaded at the same time (default 8)")
    ap.add_argument("--search-workers", type=int, default=2, help="searches at the same time (default 3)")
    ap.add_argument("--search-delay", type=float, default=1.8,
                    help="seconds each search worker rests after a search (use 3 or more if Google blocks you)")
    ap.add_argument("--skip-check", action="store_true", help="do not test Google before starting")
    ap.add_argument("--browser-ua", action="store_true",
                    help="identify as a normal web browser (only changes the User-Agent text; helps with HTTP 403 portals)")
    ap.add_argument("--list-portals", action="store_true", help="print the portal list and exit")
    ap.add_argument("--allow-no-area", action="store_true",
                    help="also keep rows where the article gives no exact area (default: skip them)")
    ap.add_argument("--geocode", action="store_true", help="add latitude/longitude via OpenStreetMap (done once at the end)")
    ap.add_argument("--out", default="")
    ap.add_argument("--dups", default="duplicates_skipped.csv")
    ap.add_argument("--rejects", default="rejected_articles.csv", help="log of articles dropped and why")
    a = ap.parse_args()

    portals = None
    if a.portals or a.portals_file:
        extra = []
        if a.portals_file:
            extra = [re.sub(r"^https?://|/.*$", "", l.strip().lower().removeprefix("www."))
                     for l in open(a.portals_file, encoding="utf-8") if l.strip() and not l.startswith("#")]
        portals = list(dict.fromkeys(PORTALS + extra))
    if a.list_portals:
        for i, pt in enumerate(portals or PORTALS, 1):
            print(f"{i:3d}. {pt}")
        return
    preflight()
    USE_BROWSER_UA[0] = a.browser_ua
    socket.setdefaulttimeout(30)          # nothing can hang forever any more
    globals()["SEARCH_DELAY"] = a.search_delay
    if not a.skip_check:
        blocked, code = google_check()
        if blocked:
            print(f"\nGoogle News is refusing this connection right now (answer: {code}).\n"
                  "Starting now would only make the block last longer, so nothing was started.\n"
                  "  1. Wait 2-3 hours (overnight is best), or switch to your phone's hotspot / turn off any VPN.\n"
                  "  2. Check again with:\n"
                  '     curl -s -o /dev/null -w "%{http_code}\\n" "https://news.google.com/rss/search?q=test&hl=bn&gl=BD&ceid=BD:bn"\n'
                  "     (200 = open again)\n"
                  "  3. Then run the same command with:  --search-workers 1 --search-delay 3\n"
                  "(Use --skip-check to start anyway.)")
            return

    start = datetime.fromisoformat(a.from_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(a.to_date).replace(tzinfo=timezone.utc) if a.to_date else datetime.now(timezone.utc)
    out = a.out or ("dhaka_chattogram_highway_accidents.csv" if a.highway else "road_accidents_dhaka_ctg.csv")
    window_days = a.window_days or (180 if a.highway else 90)

    gpath = "gnews_cache.json"
    gcache = json.load(open(gpath, encoding="utf-8")) if os.path.exists(gpath) else {}
    ppath = "processed_highway.json" if a.highway else "processed_all.json"
    proc = {}
    if os.path.exists(ppath):
        try:
            d = json.load(open(ppath, encoding="utf-8"))
            if d.get("version") == RULES_VERSION:
                proc = d.get("urls", {})
        except Exception:
            proc = {}

    load_fixes()
    clean_existing(out)
    seen_urls, seen_events, seen_titles, total = load_existing(out)
    seen_cands, seen_hashes = set(), set()
    st = {"rows": total, "added": 0, "dup": 0, "dead": 0, "skip": 0, "noarea": 0, "handled": 0, "old": 0}
    why_n = {}
    last_save = [time.time()]

    def save_caches(force=False):
        if force or time.time() - last_save[0] > 20:
            last_save[0] = time.time()
            for path, obj in ((gpath, dict(gcache)), (ppath, {"version": RULES_VERSION, "urls": dict(proc)}),
                              (TC_PATH, dict(TC)), (GC_PATH, dict(GC))):
                try:
                    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False)
                except Exception:
                    pass

    new_file = not os.path.exists(out)
    fout = open(out, "a", newline="", encoding="utf-8-sig")
    w = csv.DictWriter(fout, fieldnames=CSV_FIELDS)
    if new_file:
        w.writeheader()
    fdup = open(a.dups, "a", newline="", encoding="utf-8-sig"); wd = csv.writer(fdup)
    frej = open(a.rejects, "a", newline="", encoding="utf-8-sig"); wr = csv.writer(frej)
    target = a.rows
    done = lambda: bool(target) and st["rows"] >= target
    print(f"Output: {out} | existing rows: {total} | target: {target or 'no limit'} | "
          f"{a.workers} download workers + {a.search_workers} search workers", flush=True)
    get_gt()

    # ---------- worker: runs in a thread, touches no shared output ----------
    def work(c):
        try:
            url = c["url"]
            if c["gnews"]:
                url = resolve_gnews(url, gcache)
                if not url:
                    return {"status": "dead", "c": c}
                if canon_url(url) in seen_urls:
                    return {"status": "dup", "c": c, "final": url}
            art = fetch_article(url)
        except Exception as e:
            note_err("download", f"{type(e).__name__}: {e}")
            art = None
        if not art:
            return {"status": "dead", "c": c}
        title = art["title"] or c["title"]
        pub_dt = (c["published"] or datetime.now(timezone.utc)).astimezone(BD)
        why = []
        recs = extract_rules(title, art["text"], pub_dt.date(), highway_only=a.highway, why=why)
        res = {"status": "skip", "c": c, "art": art, "title": title, "pub": pub_dt, "why": why[0] if why else "?"}
        keep = []
        for r in recs:
            if not a.allow_no_area and not (r["subdistrict"] or r["police_station"] or r["village_area"]):
                res["why"] = "no exact area written in the article"; res["status"] = "noarea"; continue
            ev = r.pop("evidence")
            for f in ("subdistrict", "police_station", "village_area", "road_name"):
                r[f + "_original"] = r[f]
            r["subdistrict"] = translate_place(r["subdistrict"])
            r["police_station"] = translate_place(r["police_station"])
            r["village_area"] = translate_place(r["village_area"])
            r["road_name"] = translate_road(r["road_name"])
            r.update(source_name=urlparse(art["final_url"]).netloc.removeprefix("www."),
                     source_url=art["final_url"], article_published=pub_dt.date().isoformat(),
                     article_title_original=title, evidence_original=ev,
                     article_title_en=translate_text(title), evidence_en=translate_text(ev))
            keep.append(r)
        if keep:
            res["status"], res["recs"] = "rec", keep
        return res

    # ---------- main thread: dedupe + write ----------
    row_n, cand_n = {}, {}

    def handle(res):
        c, s_ = res["c"], res["status"]
        st["handled"] += 1
        if s_ == "dead":
            st["dead"] += 1; return
        if s_ == "dup":
            st["dup"] += 1; proc[c["canon"]] = "dup"; return
        art = res["art"]
        final, title = canon_url(art["final_url"]), res["title"]
        h = hashlib.sha1(norm(art["text"])[:1500].encode()).hexdigest()
        t = title_key(title)
        if final in seen_urls or h in seen_hashes or (t and t in seen_titles) or similar_title(t, seen_titles):
            wd.writerow([art["final_url"], "same article already processed"]); st["dup"] += 1
            proc[c["canon"]] = "dup"; return
        seen_urls.add(final); seen_urls.add(c["canon"]); seen_hashes.add(h); seen_titles.add(t)
        if s_ in ("skip", "noarea"):
            st["skip" if s_ == "skip" else "noarea"] += 1
            why_n[res["why"]] = why_n.get(res["why"], 0) + 1
            wr.writerow([art["final_url"], res["why"], title[:150]])
            proc[c["canon"]] = s_; return
        for r in res["recs"]:
            if is_dup_event(r, seen_events):
                wd.writerow([art["final_url"], "same accident already recorded: " + ev_key(r)]); st["dup"] += 1; continue
            seen_events.setdefault(ev_key(r), []).append(area_set(r))
            w.writerow({f: r.get(f) for f in CSV_FIELDS}); fout.flush()
            st["added"] += 1; st["rows"] += 1
            if c.get("portal"):
                row_n[c["portal"]] = row_n.get(c["portal"], 0) + 1
            print(f"   + [{st['rows']}{'/' + str(target) if target else ''}] {r['district']} | "
                  f"dead={r['death_count']} inj={r['injury_count']} | {r['article_published']} | {r['source_name']}",
                  flush=True)
        proc[c["canon"]] = "rec"

    # ---------- event loop ----------
    queue = build_queue(a.highway, start, end, portals, a.by, window_days, a.deep, a.also_general)
    print(f"{len(queue)} searches queued (windows are split automatically when a search is full).", flush=True)
    pidx = {pt: i for i, pt in enumerate(portals or [], 1)}
    s_ex, w_ex = ThreadPoolExecutor(a.search_workers), ThreadPoolExecutor(a.workers)
    s_fut, w_fut = {}, {}
    fails = pauses = 0
    last_portal, stopped_early = None, False
    zero_streak = blocks = 0
    bad_parts = set()
    zero_recent = []
    try:
        while not done():
            if RL.get("strikes", 0) >= 5:
                print("\nGoogle blocked this connection 5 times in a row. Stopping so the block can clear "
                      "(more attempts make it last longer). Progress is saved.\n"
                      "Wait 2-3 hours (overnight is best) or switch to your phone hotspot, then run the same command "
                      "with:  --search-workers 1 --search-delay 3", flush=True)
                stopped_early = True; break
            while queue and len(s_fut) < a.search_workers and len(w_fut) < a.workers * 3 and time.time() >= RL["until"]:
                qd = queue.popleft()
                if portal_dead(qd.get("portal")) or part_key(qd) in bad_parts:
                    continue                      # portal blocks downloads, or this search type ignores dates: skip
                s_fut[s_ex.submit(do_search, qd)] = qd
            if not s_fut and not w_fut:
                if queue:
                    time.sleep(1); continue
                break
            finished, _ = wait(list(s_fut) + list(w_fut), timeout=3, return_when=FIRST_COMPLETED)
            for f in finished:
                if f in s_fut:
                    qd = s_fut.pop(f)
                    res = f.result()
                    if res is None:                              # the search itself failed
                        fails += 1
                        qd["tries"] += 1
                        if qd["tries"] <= 2:
                            queue.append(qd)
                        if fails >= 8:
                            pauses += 1
                            if pauses > 3:
                                print("Search keeps failing - network or rate limit. Progress is saved; run again later.")
                                stopped_early = True; break
                            print("  pausing 2 minutes then continuing...", flush=True)
                            RL["until"] = time.time() + 120; fails = 0
                        continue
                    fails = 0
                    raw, entries = res
                    if raw == 0:
                        zero_streak += 1; zero_recent.append(qd)
                        if zero_streak >= 20:                    # 20 empty searches in a row: real silence or a block?
                            cr = do_search(CANARY)
                            if cr is None or cr[0] == 0:         # even the always-works test search is empty -> blocked
                                blocks += 1
                                if blocks > 4:
                                    print("Google keeps returning empty results (temporary block). Progress is saved. "
                                          "Try again in an hour, or on another network (e.g. phone hotspot).", flush=True)
                                    stopped_early = True; break
                                mins = 5 * blocks
                                print(f"  Google returned nothing for {zero_streak} searches in a row AND for a test search: "
                                      f"it is throttling this connection. Pausing {mins} min, then retrying them...", flush=True)
                                RL["until"] = time.time() + 60 * mins
                                queue.extendleft(reversed(zero_recent[-zero_streak:]))
                            zero_streak = 0; zero_recent.clear()
                    else:
                        zero_streak = 0; zero_recent.clear()
                    unseen = sum(1 for e in entries if e["canon"] not in seen_cands)
                    date_ignored = False
                    if qd.get("a") and len(entries) >= 8:
                        lo, hi = qd["a"] - timedelta(days=3), qd["b"] + timedelta(days=3)
                        outside = sum(1 for e in entries if e["published"] and not lo <= e["published"].date() <= hi)
                        date_ignored = outside > 0.5 * len(entries)
                    if date_ignored:                              # Google returned articles from all dates: more splitting is pointless
                        bad_parts.add(part_key(qd))
                        note_err("search", f"Google ignores the dates for {qd['label'][:50]} - skipping the rest of this search type")
                    elif (qd.get("splittable") and raw >= FULL_PAGE and unseen >= 12
                            and (qd["b"] - qd["a"]).days > MIN_WINDOW_DAYS):
                        queue.extendleft(reversed(split_query(qd)))   # newer half is searched first
                    cands = []
                    for e in entries:
                        if e["canon"] in seen_cands or (e["published"] and e["published"] < start):
                            continue
                        if e["published"] and e["published"] > end + timedelta(days=1):
                            continue                              # newer than --to-date: not wanted in this run
                        seen_cands.add(e["canon"])
                        v = proc.get(e["canon"])
                        if v in ("skip", "rec", "dup") or (v == "noarea" and not a.allow_no_area):
                            st["old"] += 1; continue              # judged in an earlier run
                        cands.append(e)
                    pk = qd.get("portal")
                    if pk:
                        cand_n[pk] = cand_n.get(pk, 0) + len(cands)
                        if a.by == "portal" and pk != last_portal:
                            print(f"\n=== Portal {pidx[pk]}/{len(portals)}: {pk} ===", flush=True); last_portal = pk
                    print(f"[search] {qd['label'][:60]} -> {raw} found, {len(cands)} new | queue {len(queue)} | "
                          f"rows {st['rows']}{'/' + str(target) if target else ''}", flush=True)
                    for e in cands:
                        w_fut[w_ex.submit(work, e)] = e
                else:
                    w_fut.pop(f)
                    try:
                        handle(f.result())
                    except Exception as ex:
                        st["dead"] += 1
                        print(f"  (article error {type(ex).__name__}: {str(ex)[:80]})", flush=True)
                    if st["handled"] % 25 == 0:
                        print(f"   ... {st['handled']} articles read | kept {st['added']} | no-area {st['noarea']} | "
                              f"not-a-report {st['skip']} | dup {st['dup']} | unreachable {st['dead']} | "
                              f"waiting {len(w_fut)}", flush=True)
                    save_caches()
            if stopped_early:
                break
        if not stopped_early and not done() and target:
            print(f"\nSearched back to {a.from_date}; only {st['rows']}/{target} rows found. "
                  f"Use an earlier --from-date to go further back.")
    except KeyboardInterrupt:
        print("\nStopped by you. Progress is saved.", flush=True)
    finally:
        s_ex.shutdown(wait=False, cancel_futures=True)
        w_ex.shutdown(wait=False, cancel_futures=True)
        for f in w_fut:                      # keep finished-but-unhandled results
            if f.done() and not f.cancelled():
                try:
                    handle(f.result())
                except Exception:
                    pass
        fout.close(); fdup.close(); frej.close()
        save_caches(force=True)

    finalize_places(out)
    if a.geocode:
        geocode_pass(out)
    if portals:
        print("\nPer-portal report (new articles read / rows saved):")
        for pt in portals:
            n = cand_n.get(pt)
            note = "" if n else "   <- nothing found (not in the Google News index, or not reached yet)"
            print(f"  {pt:34s} {n or 0:5d} / {row_n.get(pt, 0):4d}{note}")
        json.dump({pt: {"articles": cand_n.get(pt, 0), "rows": row_n.get(pt, 0)} for pt in portals},
                  open("portal_report.json", "w", encoding="utf-8"), indent=1)
    try:
        yrs = {}
        for r in read_rows(out):
            y = (r.get("article_published") or "")[:4] or "?"
            yrs[y] = yrs.get(y, 0) + 1
        print("\nRows saved per year (article date):")
        for y in sorted(yrs):
            print(f"  {y}: {yrs[y]}")
    except Exception:
        pass
    if why_n:
        print("\nWhy articles were not saved:")
        for k, v in sorted(why_n.items(), key=lambda x: -x[1]):
            print(f"  {v:5d}  {k}")
        print(f"  (details: {a.rejects})")
    print(f"\nDone. total rows in CSV={st['rows']} (added this run={st['added']}) | duplicates skipped={st['dup']} "
          f"| unreachable={st['dead']} | already judged earlier={st['old']} | not-a-clear-report={st['skip']} "
          f"| no-exact-area={st['noarea']}\nCSV: {out}", flush=True)

if __name__ == "__main__":
    code = 0
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr); code = 1
        elif e.code:
            code = e.code
    except Exception:
        import traceback; traceback.print_exc(); code = 1
    finally:
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(code)      # do not wait for background download threads after Ctrl+C / finish
