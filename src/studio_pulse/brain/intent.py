"""What a question asks for, read independently of how it will be answered.

The router uses this to apply filters; consistency.py uses it to check that
the evidence of an answer covers exactly what was asked (the CPI-on-Meta
lesson: an answer whose numbers verify against its evidence can still answer
another question when a filter was dropped on the way).

Extracted: the apps (ids, display names, typos within a small edit distance,
genre words such as "the puzzle game"), names that look like an app but are
not one, ad networks, platforms, stores, countries, app versions, device
tiers, level numbers, the period (yesterday, a date, a month, last N days,
"last week", "since ..."), metric words the catalog does not know (NPS,
K-factor), and the question's shape (why, trend, comparison, ranking, total).
Turkish suffixes after an apostrophe or glued to a name ("Meta'da",
"Türkiye'deki", "iOS'ta", "Almanyada") are tolerated.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .catalog import normalize

# canonical ad network id -> phrases (normalized). Values as the UA connectors store them.
NETWORKS: dict[str, tuple[str, ...]] = {
    "meta": ("meta", "facebook", "fb", "instagram", "meta ads", "facebook ads"),
    "google_ads": ("google ads", "adwords", "google uac", "uac", "google"),
    "tiktok": ("tiktok", "tik tok", "pangle"),
    "applovin": ("applovin", "app lovin"),
    "unity_ads": ("unity ads", "unity"),
    "ironsource": ("ironsource", "iron source"),
    "mintegral": ("mintegral",),
    "snapchat": ("snapchat",),
    "apple_search_ads": ("apple search ads", "apple ads", "search ads"),
    "liftoff": ("liftoff", "vungle"),
    "moloco": ("moloco",),
    "reddit": ("reddit ads", "reddit"),
    "pinterest": ("pinterest",),
    "organic": ("organic", "organik"),
}
NETWORK_NAMES = {
    "meta": "Meta", "google_ads": "Google Ads", "tiktok": "TikTok", "applovin": "AppLovin",
    "unity_ads": "Unity Ads", "ironsource": "ironSource", "mintegral": "Mintegral",
    "snapchat": "Snapchat", "apple_search_ads": "Apple Search Ads", "liftoff": "Liftoff",
    "moloco": "Moloco", "reddit": "Reddit", "pinterest": "Pinterest", "organic": "organic",
}  # fmt: skip
# values the same source has in other tables (GA4 first-touch `traffic_source.source`)
NETWORK_ALIASES: dict[str, tuple[str, ...]] = {
    "meta": ("meta", "facebook", "fb", "instagram", "ig"),
    "google_ads": ("google_ads", "google", "googleads", "adwords"),
    "tiktok": ("tiktok", "pangle"),
    "applovin": ("applovin",),
    "unity_ads": ("unity_ads", "unity", "unityads"),
    "ironsource": ("ironsource",),
    "organic": ("organic",),
}

STORES: dict[str, tuple[str, ...]] = {
    "google_play": ("google play", "play store", "play console", "play"),
    "app_store": ("app store", "apple app store", "itunes"),
}
PLATFORMS: dict[str, tuple[str, ...]] = {
    "IOS": ("ios", "iphone", "ipad", "apple"),
    "ANDROID": ("android",),
    "WEB": ("web", "webgl", "browser", "tarayici"),
    "DESKTOP": ("desktop", "pc", "windows", "macos", "steam", "masaustu"),
    "CONSOLE": ("console", "playstation", "xbox", "switch", "konsol"),
}
TIERS: dict[str, tuple[str, ...]] = {
    "low": (
        "low end", "low tier", "low-tier", "low-end", "cheap phones", "cheap phone", "budget phones",
        "low end devices", "dusuk seviye", "dusuk sinif", "ucuz telefon", "dusuk segment",
    ),
    "mid": ("mid tier", "mid range", "mid-range", "orta seviye", "orta sinif"),
    "high": ("high end", "high tier", "high-end", "flagship", "yuksek seviye", "ust seviye", "yuksek sinif"),
    "tablet": ("tablet", "tablets", "tabletler", "tabletlerde", "ipad"),
}  # fmt: skip

# ISO 3166 alpha-2 -> (English name as GA4 writes it, other English names, Turkish names)
COUNTRIES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "US": ("United States", ("usa", "u s", "united states", "america", "the us", "the states"), ("abd", "amerika", "amerika birlesik devletleri")),
    "TR": ("Turkey", ("turkey", "turkiye"), ("turkiye",)),
    "DE": ("Germany", ("germany",), ("almanya",)),
    "GB": ("United Kingdom", ("united kingdom", "uk", "britain", "great britain", "england"), ("ingiltere", "birlesik krallik")),
    "FR": ("France", ("france",), ("fransa",)),
    "JP": ("Japan", ("japan",), ("japonya",)),
    "BR": ("Brazil", ("brazil", "brasil"), ("brezilya",)),
    "IN": ("India", ("india",), ("hindistan",)),
    "ID": ("Indonesia", ("indonesia",), ("endonezya",)),
    "MX": ("Mexico", ("mexico",), ("meksika",)),
    "ES": ("Spain", ("spain",), ("ispanya",)),
    "IT": ("Italy", ("italy",), ("italya",)),
    "CA": ("Canada", ("canada",), ("kanada",)),
    "AU": ("Australia", ("australia",), ("avustralya",)),
    "KR": ("South Korea", ("south korea", "korea"), ("guney kore", "kore")),
    "CN": ("China", ("china",), ("cin",)),
    "RU": ("Russia", ("russia",), ("rusya",)),
    "NL": ("Netherlands", ("netherlands", "holland"), ("hollanda",)),
    "SA": ("Saudi Arabia", ("saudi arabia", "saudi"), ("suudi arabistan",)),
    "PL": ("Poland", ("poland",), ("polonya",)),
    "PH": ("Philippines", ("philippines",), ("filipinler",)),
    "VN": ("Vietnam", ("vietnam",), ("vietnam",)),
    "TH": ("Thailand", ("thailand",), ("tayland",)),
    "EG": ("Egypt", ("egypt",), ("misir",)),
    "AR": ("Argentina", ("argentina",), ("arjantin",)),
    "UA": ("Ukraine", ("ukraine",), ("ukrayna",)),
    "AZ": ("Azerbaijan", ("azerbaijan",), ("azerbaycan",)),
}  # fmt: skip
# upper-case codes that are also analytics words (UA = user acquisition, AR, ID, IT, CA, PH,
# SA, EG, NL, PL, AZ, TH, VN): a country only when written as a name
_ISO_AMBIGUOUS = frozenset(
    {"UA", "ID", "IT", "AR", "CA", "PH", "SA", "EG", "NL", "PL", "AZ", "TH", "VN"}
)
# words that are both a country-ish token and something else: matched only as written
_ISO_UPPER = re.compile(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])")

AD_FORMATS: dict[str, tuple[str, ...]] = {
    "rewarded": ("rewarded", "rewarded ads", "rewarded video", "odullu", "odullu reklam"),
    "interstitial": ("interstitial", "interstitials", "gecis reklami", "tam ekran reklam"),
    "banner": ("banner", "banners"),
}
# "which network", "by country", "hangi ağ", "ülkeye göre": the dimension a ranking or split asks for
_BY = {
    "network": r"(?:which|what|by|per|each) (?:ad )?networks?|(?:hangi|her) (?:reklam )?ag\w*|aglara gore|ag bazinda",
    "app_id": r"which (?:app|game)s?|what (?:app|game)|(?:hangi|her) (?:uygulama|oyun)\w*|uygulamalara gore",
    "level": r"which levels?|what levels?|(?:hangi|her) (?:seviye|bolum)\w*|levels? with the (?:lowest|highest|most)",
    "country": r"which countr\w+|by country|per country|hangi ulke\w*|ulkeye gore",
    "platform": r"which platform\w*|by platform|hangi platform\w*|platforma gore",
    "app_version": r"which (?:app )?versions?|by version|hangi surum\w*|surume gore",
    "creative_key": r"which creatives?|hangi kreatif\w*|best creative|en iyi kreatif",
    "product_id": r"which products?|hangi urun\w*|(?:top|best[- ]selling|most) products?|en cok (?:satan|gelir getiren) urun\w*",
    "topic": r"complain\w*|sikayet\w*|sikâyet\w*|which topics?",
    "campaign_name": r"which campaigns?|by campaign|hangi kampanya\w*",
    "setting": r"which settings?|hangi ayar\w*",
    "ad_format": r"which (?:ad )?formats?|by (?:ad )?format|hangi reklam bicimi\w*",
}


def _by_dims(norm: str) -> list[str]:
    return [d for d, rx in _BY.items() if re.search(r"\b(?:" + rx + r")\b", norm)]


UNKNOWN_METRICS: dict[str, tuple[str, ...]] = {
    "NPS": ("nps", "net promoter"),
    "K-factor": (
        "k factor",
        "k faktoru",
        "k faktor",
        "viral katsayi",
        "viral coefficient",
        "virality",
    ),
    "CSAT": ("csat", "customer satisfaction"),
    "LTV/CAC": ("ltv/cac", "ltv cac"),
    "MRR": ("mrr", "monthly recurring revenue"),
}
_GENRES = {
    "puzzle": ("puzzle", "bulmaca"),
    "casual": ("casual",),
    "fitness": ("fitness",),
}

_WHY = re.compile(
    r"\b(why|how come|what caused|what s behind|what is behind|what drove|cause of|reason|"
    r"neden|niye|nicin|sebebi\w*|nedeni\w*)\b"
)
_TREND = re.compile(
    r"\b(trend\w*|getting (?:worse|better)|improv\w*|worse\w*|better than|increas\w*|decreas\w*|"
    r"grow\w*|declin\w*|going (?:up|down)|more than before|less than before|week over week|"
    r"wow|month over month|change[ds]?|changing|kotules\w*|iyiles\w*|artiyor\w*|azaliyor\w*|"
    r"dusuyor\w*|yukseliyor\w*|egilim\w*|gidisat\w*|eskisinden|degisti\w*|degisim\w*|"
    r"gecen haftaya gore|onceki haftaya gore)\b"
)
_COMPARE = re.compile(r"\b(compare\w*|vs|versus|karsilastir\w*|kiyasla\w*)\b")
_RANK = re.compile(
    r"\b(which|what app|hangi\w*|top|best|worst|most|least|highest|lowest|cheapest|biggest|"
    r"en (?:iyi|kotu|cok|az|yuksek|dusuk|ucuz|pahali|sik|zor|kolay))\b"
)
_TOTAL = re.compile(r"\b(total|combined|altogether|sum of|toplam\w*|hepsi birlikte)\b")
_ALL_APPS = re.compile(
    r"\b(across all|all (?:of )?(?:our |the )?(?:apps|games)|every app|portfolio|"
    r"tum uygulama\w*|butun uygulama\w*|tum oyun\w*|butun oyun\w*)\b"
)
_LEVEL = re.compile(
    r"\b(?:level|seviye|bolum)\s*(\d{1,4})\b|\b(\d{1,4})\s*\.?\s*(?:seviye|bolum|level)\w*"
)
_VERSION = re.compile(r"(?<![\w.])v?(\d{1,3}\.\d{1,3}\.\d{1,4})(?![\w.])")
_VERSION2 = re.compile(r"\b(?:version|versiyon|surum\w*|v)\s*(\d{1,3}\.\d{1,3})(?![\d.])")
_DAY_N = re.compile(r"\b(?:d|day ?-?|gun ?)(\d{1,3})\b|\b(\d{1,3})\s*\.?\s*(?:gun|day)\b")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7,
    "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
}  # fmt: skip
_MONTH_WORD = r"(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")[a-z]{0,6}"
_DAY_MONTH = re.compile(r"\b(\d{1,2})\s+" + _MONTH_WORD + r"(?:\s+(20\d\d))?\b")
_MONTH_DAY = re.compile(r"\b" + _MONTH_WORD + r"\s+(\d{1,2})(?:\s+(20\d\d))?\b")
_MONTH_ONLY = re.compile(r"\b" + _MONTH_WORD + r"(?:\s+(20\d\d))?\b")
_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "ten": 10, "twelve": 12, "fourteen": 14, "thirty": 30, "bir": 1, "iki": 2, "uc": 3,
    "dort": 4, "bes": 5, "alti": 6, "yedi": 7, "sekiz": 8, "on": 10, "otuz": 30,
}  # fmt: skip
_LAST_N = re.compile(
    r"\b(?:last|past|previous|son|gecen|over the last|over the past)\s+(\d{1,3}|"
    + "|".join(_NUMBER_WORDS)
    + r")\s*(days?|weeks?|months?|gun\w*|hafta\w*|ay\w*)\b"
)
# "within 7 days", "by day 30", "in the first 14 days", "ilk 7 günde", "7 gün içinde": day N
_WITHIN = re.compile(
    r"\b(?:within|by day|in the first|first|ilk)\s+(\d{1,3})(?:\s*(?:days?|gun\w*))?\b|"
    r"\b(\d{1,3})\s*gun (?:icinde|icerisinde)\b"
)
_YESTERDAY = re.compile(r"\b(yesterday|dun|dunku|dunun)\b")
_TODAY = re.compile(r"\b(today|bugun\w*)\b")
_LAST_WEEK = re.compile(
    r"\b(last week|past week|previous week|this past week|gecen hafta\w*|son hafta\w*)\b"
)
_THIS_WEEK = re.compile(r"\b(this week|bu hafta\w*)\b")
_LAST_MONTH = re.compile(
    r"\b(last month|past month|previous month|gecen ay\w*|son ay\w*|son bir ay\w*)\b"
)
_THIS_MONTH = re.compile(r"\b(this month|bu ay\w*)\b")
_LATEST = re.compile(r"\b(latest|current|currently|right now|now|en son|su an\w*|guncel)\b")
_SINCE = re.compile(r"\b(?:since)\b")
_FUTURE = re.compile(
    r"\b(next (?:week|month|year|quarter)|tomorrow|will (?:\w+ )?be|going to be|forecast\w*|"
    r"gelecek (?:hafta|ay|yil)\w*|yarin\w*|onumuzdeki \w+|ne olacak|olacak)\b"
)
COMMON_WORDS = set(
    """a an the of in on at for to by and or vs with from per each our my your their this that these
    those what which who why how when where is are was were be been do does did can could would should
    will give show list tell me us compare average total overall all any some most least best worst
    did does do has have had get got make made earn earned spend spent much
    top bottom highest lowest new current latest last past previous next yesterday today week month
    year day days weeks months share rate number count much many users players people app apps game
    games revenue spend installs install retention crash crashes level levels please about there
    rating store stores ad ads network networks country countries version versions device devices
    tier tiers phone phones platform platforms daily weekly monthly cost per install ltv dau mau wau
    ve ile icin mi mu ne nedir nasil hangi hangisi kac kadar en son gecen bu su tum butun uygulama
    oyun oyuncu kullanici gelir harcama kurulum seviye gun hafta ay yil oran orani""".split()
)
_WORDS_NOT_APPS = {
    "how", "what", "which", "why", "when", "where", "who", "is", "are", "the", "give", "list",
    "show", "compare", "did", "does", "do", "can", "average", "total", "per", "and", "or", "of",
    "d1", "d7", "d30", "dau", "mau", "wau", "cpi", "ctr", "ipm", "roas", "arpdau", "ltv", "ecpm",
    "anr", "nps", "srm", "p95", "p50", "iap", "ua", "sdk", "ga4", "a", "b", "i", "q3", "q4",
}  # fmt: skip


@dataclass
class Period:
    start: dt.date | None = None
    end: dt.date | None = None
    days: int | None = None  # a relative length: "last 7 days", "last week"
    label: str = ""
    latest: bool = False  # "current", "en son": the newest value

    def resolve(self, last: dt.date) -> tuple[dt.date, dt.date] | None:
        """Concrete [start, end], a relative period ending at `last` (the data's newest day)."""
        if self.start and self.end:
            return self.start, self.end
        if self.days:
            return last - dt.timedelta(days=self.days - 1), last
        return None


@dataclass
class Intent:
    question: str
    norm: str
    apps: list[str] = field(default_factory=list)  # resolved app ids, in order of mention
    typo_apps: dict[str, str] = field(default_factory=dict)  # written form -> app id
    unknown_apps: list[str] = field(default_factory=list)  # names that look like an app
    genre_apps: list[str] = field(default_factory=list)  # "the puzzle game" -> its apps
    networks: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    stores: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)  # ISO alpha-2
    versions: list[str] = field(default_factory=list)
    tiers: list[str] = field(default_factory=list)
    levels: list[int] = field(default_factory=list)
    day_ns: list[int] = field(default_factory=list)
    ad_formats: list[str] = field(default_factory=list)
    by: list[str] = field(default_factory=list)  # "which network", "by country": a breakdown
    period: Period | None = None
    future: bool = False
    unknown_metrics: list[str] = field(default_factory=list)
    why: bool = False
    trend: bool = False
    compare: bool = False
    rank: bool = False
    total: bool = False  # "total", "toplam": one number
    all_apps: bool = False  # "across all apps", "tüm uygulamalar"

    def filters(self) -> dict[str, list[str]]:
        """Dimension -> requested values (canonical ids) for every filter the question names."""
        out: dict[str, list[str]] = {}
        for dim, vals in (
            ("network", self.networks),
            ("platform", self.platforms),
            ("store", self.stores),
            ("country", self.countries),
            ("app_version", self.versions),
            ("device_tier", self.tiers),
            ("level", [str(x) for x in self.levels]),
            ("ad_format", self.ad_formats),
        ):
            if vals:
                out[dim] = list(vals)
        return out


# Turkish case endings a name may carry without an apostrophe ("almanyada", "metadaki")
_TR_SUFFIX = (
    r"(?:'?(?:daki|deki|taki|teki|dan|den|tan|ten|da|de|ta|te|ya|ye|nin|nun|in|un|a|e|i|u))?"
)


def _rx(phrase: str) -> str:
    glue = _TR_SUFFIX if len(phrase.replace(" ", "")) >= 4 and " " not in phrase else ""
    return r"(?<= )" + re.escape(phrase) + glue + r"(?= )"


def _phrase_in(phrase: str, padded: str) -> bool:
    """Phrase as whole words; a Turkish case ending may be glued to a one-word name of four
    letters or more ("almanyada"). After an apostrophe the ending is a word of its own
    ("Meta'da" -> "meta da"), so nothing else is needed."""
    return re.search(_rx(phrase), padded) is not None


def _phrase_exact(phrase: str, padded: str) -> bool:
    return f" {phrase} " in padded


def _lev(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def find_apps(norm: str, apps: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Exact mentions (id or display name) and typos (edit distance 1, 2 for long names)."""
    padded = f" {norm} "
    hits: list[tuple[int, str]] = []
    for app_id, name in apps.items():
        for form in {normalize(app_id), normalize(name), normalize(app_id).replace(" ", "")}:
            if not form:
                continue
            i = padded.find(f" {form} ")
            if i < 0:  # glued Turkish suffix: "pebblepopun", "quartz mergeun"
                m = re.search(
                    r"(?<= )" + re.escape(form) + r"(?:un|un|in|nin|nun|in|a|e|ta|te|da|de)(?= )",
                    padded,
                )
                i = m.start() if m else -1
            if i >= 0:
                hits.append((i, app_id))
    seen: list[str] = []
    for _, a in sorted(hits):
        if a not in seen:
            seen.append(a)
    typos: dict[str, str] = {}
    if not seen:
        toks = norm.split()
        grams = set()
        for n in (1, 2, 3):
            for i in range(len(toks) - n + 1):
                g = toks[i : i + n]
                grams.add(" ".join(g))
                grams.add("".join(g))
        best: dict[str, tuple[int, str]] = {}
        for app_id, name in apps.items():
            forms = {normalize(app_id), normalize(name), normalize(app_id).replace(" ", "")}
            for g in grams:
                if len(g) < 6 or g in _WORDS_NOT_APPS:
                    continue
                for form in forms:
                    if len(form) < 6:
                        continue
                    cap = 2 if len(form) >= 10 else 1
                    d = _lev(g, form, cap)
                    if 0 < d <= cap and (app_id not in best or d < best[app_id][0]):
                        best[app_id] = (d, g)
        if len(best) == 1:  # one candidate only: a typo; several: ambiguous, no guess
            app_id, (_d, g) = next(iter(best.items()))
            typos[g] = app_id
            seen.append(app_id)
    return seen, typos


_SNAKE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_CAPS = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")


def unknown_app_names(
    question: str, apps: dict[str, str], known_terms: set[str], known_ids: set[str]
) -> list[str]:
    """Names written like an app (snake_case, or Two Capitalised Words) that no app has."""
    app_forms = {normalize(a) for a in apps} | {normalize(n) for n in apps.values()}
    out = []
    for m in _SNAKE.finditer(question.lower()):
        s = m.group(1)
        if s in known_ids or normalize(s) in app_forms or s in apps:
            continue
        if any(_lev(normalize(s), f, 2) <= 2 for f in app_forms):
            continue  # a typo of a real app, handled by find_apps
        out.append(s)
    for m in _CAPS.finditer(question):
        s = m.group(1)
        initial = m.start() == 0 or question[: m.start()].rstrip().endswith((".", "?", "!", ":"))
        if initial and normalize(s.split()[0]) in COMMON_WORDS:
            s = s.split(None, 1)[1]  # "What Galaxy ..." -> "Galaxy ..."; "Galaxy Run" stays
            if " " not in s:
                continue
        n = normalize(s)
        if n in app_forms or n in known_terms:
            continue
        words = n.split()
        if any(w in known_terms or w in COMMON_WORDS for w in words) or any(
            _lev(n, f, 2) <= 2 for f in app_forms
        ):
            continue
        out.append(s)
    return out


def _countries(question: str, padded: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for iso, (en, others, trs) in COUNTRIES.items():
        for name in {normalize(en), *others, *trs}:
            m = re.search(_rx(name), padded)
            if m and iso not in [c for _, c in found]:
                found.append((m.start(), iso))
                break
    for m in _ISO_UPPER.finditer(question):
        code = m.group(1)
        if code in COUNTRIES and code not in [c for _, c in found] and code not in _ISO_AMBIGUOUS:
            if code == "US" and re.search(r"\bUS\b", question) is None:
                continue
            found.append((len(padded), code))
    return [c for _, c in sorted(found)]


def parse_period(norm: str, as_of: dt.date) -> tuple[Period | None, bool]:
    """(period, future): what time the question names, relative to `as_of` (today)."""
    yesterday = as_of - dt.timedelta(days=1)
    future = bool(_FUTURE.search(norm))

    def month_range(month: int, year: int | None) -> tuple[dt.date, dt.date]:
        y = year or (as_of.year if month <= as_of.month else as_of.year - 1)
        first = dt.date(y, month, 1)
        nxt = dt.date(y + (month == 12), month % 12 + 1, 1)
        return first, nxt - dt.timedelta(days=1)

    dates: list[dt.date] = []
    for m in _ISO.finditer(norm):
        try:
            dates.append(dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    for rx, di, mi, yi in ((_DAY_MONTH, 1, 2, 3), (_MONTH_DAY, 2, 1, 3)):
        for m in rx.finditer(norm):
            mon = _MONTHS[m.group(mi)]
            year = int(m.group(yi)) if m.group(yi) else None
            try:
                d = dt.date(year or as_of.year, mon, int(m.group(di)))
            except ValueError:
                continue
            if year is None and d >= as_of and mon > as_of.month:
                d = d.replace(year=d.year - 1)
            dates.append(d)
    if dates:
        dates = sorted(set(dates))
        if _SINCE.search(norm) or re.search(r"\b(den|dan|ten|tan) (beri|bu yana)\b", norm):
            p = Period(dates[0], yesterday, label="since")
        else:
            p = Period(dates[0], dates[-1], label="date" if len(dates) == 1 else "between")
        return p, future or p.start >= as_of
    m = _LAST_N.search(_WITHIN.sub(" ", norm))
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUMBER_WORDS[m.group(1)]
        unit = m.group(2)
        days = n * (
            7
            if unit.startswith(("week", "hafta"))
            else 30
            if unit.startswith(("month", "ay"))
            else 1
        )
        return Period(days=max(1, min(days, 400)), label=f"last {days} days"), future
    if _YESTERDAY.search(norm):
        return Period(yesterday, yesterday, label="yesterday"), future
    if _TODAY.search(norm):
        return Period(as_of, as_of, label="today"), future
    if _THIS_MONTH.search(norm):
        first = as_of.replace(day=1)
        nxt = dt.date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        return Period(
            first, nxt - dt.timedelta(days=1), label="this month"
        ), future or first >= as_of
    if _LAST_WEEK.search(norm) or _THIS_WEEK.search(norm):
        return Period(days=7, label="last week"), future
    if _LAST_MONTH.search(norm):
        return Period(days=30, label="last month"), future
    for m in _MONTH_ONLY.finditer(norm):
        word = m.group(1)
        if word in (
            "may",
            "mar",
            "jun",
            "jan",
            "dec",
            "nov",
            "oct",
            "sep",
            "aug",
            "apr",
            "feb",
            "jul",
        ):
            if word == "may" and not m.group(2):
                continue  # "may" the verb: the month only with a year
            if not re.search(r"\b(in|during|of|for)\s+" + word + r"\b", norm) and not m.group(2):
                continue  # "mar" in other words: only with a preposition or a year
        year = int(m.group(2)) if m.group(2) else None
        a, b = month_range(_MONTHS[word], year)
        return Period(a, b, label="month"), future or a >= as_of
    if _LATEST.search(norm):
        return Period(latest=True, label="latest"), future
    return None, future


def parse(
    question: str,
    *,
    apps: dict[str, str],
    as_of: dt.date,
    genres: dict[str, str] | None = None,
    known_terms: set[str] | None = None,
    known_ids: set[str] | None = None,
) -> Intent:
    q = (question or "").strip()
    norm = normalize(q)
    padded = f" {norm} "
    it = Intent(q, norm)
    it.apps, it.typo_apps = find_apps(norm, apps)
    if not it.apps and genres:
        for genre, words in _GENRES.items():
            if any(_phrase_in(w, padded) for w in words):
                it.genre_apps = sorted(a for a, g in genres.items() if g == genre)
    terms = set(known_terms or ())
    terms |= {normalize(v) for v in NETWORK_NAMES.values()}
    it.unknown_apps = unknown_app_names(q, apps, terms, set(known_ids or ())) if not it.apps else []
    # stores before platforms and networks: "Google Play" is a store, not Google Ads
    taken = padded
    for sid, phrases in STORES.items():
        for p in phrases:
            if p == "play" and not re.search(r"\b(google play|play store|play console)\b", norm):
                continue
            if _phrase_in(p, taken):
                it.stores.append(sid)
                taken = re.sub(_rx(p), " ", taken)
                break
    for nid, phrases in NETWORKS.items():
        for p in sorted(phrases, key=len, reverse=True):
            if _phrase_in(p, taken):
                if (
                    nid == "unity_ads"
                    and p == "unity"
                    and not re.search(
                        r"\b(ads?|cpi|ipm|ctr|spend|harca\w*|install\w*|kurulum\w*|reklam\w*|roas)\b",
                        norm,
                    )
                ):
                    continue
                it.networks.append(nid)
                taken = re.sub(_rx(p), " ", taken)
                break
    for pid, phrases in PLATFORMS.items():
        if any(_phrase_in(p, taken) for p in phrases):
            it.platforms.append(pid)
    for tid, phrases in TIERS.items():
        if any(_phrase_in(p, taken) for p in phrases):
            it.tiers.append(tid)
    if "tablet" in it.tiers and "IOS" in it.platforms and "ipad" in norm and "ios" not in norm:
        it.platforms.remove("IOS")
    it.countries = _countries(q, taken)
    it.versions = list(dict.fromkeys(_VERSION.findall(norm) + _VERSION2.findall(norm)))
    vmask = _VERSION.sub(" ", norm)
    it.levels = [int(a or b) for a, b in _LEVEL.findall(vmask)]
    within = [int(a or b) for a, b in _WITHIN.findall(vmask)]
    dmask = _LAST_N.sub(" ", _LEVEL.sub(" ", _WITHIN.sub(" ", vmask)))
    it.day_ns = within + [int(a or b) for a, b in _DAY_N.findall(dmask)]
    for fid, phrases in AD_FORMATS.items():
        if any(_phrase_in(p, padded) for p in phrases):
            it.ad_formats.append(fid)
    it.by = _by_dims(norm)
    it.period, it.future = parse_period(_VERSION.sub(" ", norm), as_of)
    for name, phrases in UNKNOWN_METRICS.items():
        if any(_phrase_in(p, padded) for p in phrases):
            it.unknown_metrics.append(name)
    it.why = bool(_WHY.search(norm))
    it.trend = bool(_TREND.search(norm)) or it.why
    it.compare = bool(_COMPARE.search(norm)) or len(it.apps) >= 2
    it.rank = bool(_RANK.search(norm))
    it.total = bool(_TOTAL.search(norm))
    it.all_apps = bool(_ALL_APPS.search(norm))
    return it
