import re

ALLOWED_DOMAINS = frozenset(
    {
        "wildberries.ru",
        "wildberries.by",
        "wildberries.kz",
        "wb.ru",
        "ozon.ru",
        "ozon.by",
        "ozon.kz",
    }
)
OZON_DOMAINS = ("ozon.ru", "ozon.by", "ozon.kz")
WB_DOMAINS = ("wildberries.ru", "wildberries.by", "wildberries.kz", "wb.ru")
WB_COOKIE_DOMAINS = (
    "wildberries.by",
    "wildberries.ru",
    "wb.ru",
    "card.wb.ru",
    "search.wb.ru",
    "u-card.wb.ru",
)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

PREVIEW_USER_AGENTS = (
    "TelegramBot (like TwitterBot)",
    "vkShare; (+http://vk.com/dev/Share)",
    "WhatsApp/2.24.1.6 i",
    BROWSER_USER_AGENT,
)

OZON_APP_HEADERS = {
    "User-Agent": "ozonapp_android/17.48.0+2540",
    "x-o3-app-name": "ozonapp_android",
    "x-o3-app-version": "17.48.0(2540)",
    "x-o3-device-type": "mobile",
    "x-o3-sample-trace": "false",
    "Accept": "application/json; charset=utf-8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

DEFAULT_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "max-age=0",
    "Sec-CH-UA": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Минимальный, чистый init-скрипт без переопределения own-свойств на объекте navigator
MINIMAL_STEALTH_SCRIPT = """
(() => {
    if (!window.chrome) {
        const chromeObj = {
            app: {
                isInstalled: false,
                InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
            },
            runtime: {
                OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
                OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
                PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
                RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }
            },
            loadTimes: function() { return {}; },
            csi: function() { return {}; }
        };
        Object.defineProperty(window, 'chrome', {
            value: chromeObj,
            writable: true,
            enumerable: true,
            configurable: false
        });
    }
})();
"""

MAX_FLOOD_RETRIES = 3
HTTP_RETRY_ATTEMPTS = 3
HEAD_READ_LIMIT = 450_000
IMAGE_READ_LIMIT = 8_000_000
INITIAL_POST_CHECK_INTERVAL_SECONDS = 15 * 60
INITIAL_STOCK_CHECK_INTERVAL_SECONDS = 60 * 60
MAX_POST_CHECK_INTERVAL_SECONDS = 8 * 60 * 60
POST_AUTO_DELETE_SECONDS = 24 * 60 * 60

URL_RE = re.compile(r"https?://[^\s<>\[\]\(\)\"']+", re.IGNORECASE)

# Поддержка любых поддоменов wildberries.(ru|by|kz), короткого домена wb.ru, путей /catalog/<sku> и ?card=<sku> / ?nm=<sku>
WB_SKU_RE = re.compile(
    r"(?:[a-z0-9-]+\.)?(?:wildberries\.(?:ru|by|kz)|wb\.ru)/"
    r"(?:catalog/(\d+)|product/?\?(?:[^#\"'\s<>]*&)?card=(\d+)|[^#\"'\s<>]*?[?&]nm=(\d+))",
    re.IGNORECASE,
)

WB_BASKET_HOST_IN_URL_RE = re.compile(
    r"https?://(basket-\d+\.(?:wbbasket\.ru|wbcontent\.net))/vol(\d+)/",
    re.IGNORECASE,
)

# Глобальный кэш соответствия vol -> basket-XX.wbbasket.ru (обновляется динамически при пробинге и из браузера)
WB_BASKET_VOL_CACHE: dict[int, str] = {}

# Поддержка артикулов Ozon и идентификаторов карточек /p/o<id>
OZON_FULL_SKU_RE = re.compile(
    r"(?:[a-z0-9-]+\.)?ozon\.(?:ru|by|kz)/(?:product/(?:[^/?#\"'\s<>\\]*-)?|context/detail/id/)(\d{5,15})(?:[/?#\"'\s<>\\]|$)",
    re.IGNORECASE,
)
OZON_SKU_RE = re.compile(
    r"(?:[a-z0-9-]+\.)?ozon\.(?:ru|by|kz)/(?:product/(?:[^/?#\"'\s<>\\]*-)?|context/detail/id/)(\d{5,15})(?:[/?#\"'\s<>\\]|$)|"
    r"(?:[a-z0-9-]+\.)?ozon\.(?:ru|by|kz)/p/(o\d{5,15})(?:[/?#\"'\s<>\\]|$)",
    re.IGNORECASE,
)
OZON_SHORT_PRODUCT_ID_RE = re.compile(r"o\d{5,15}", re.IGNORECASE)

# Поиск полного пути /product/<slug>-<sku>/ в теле ответа/JSON
OZON_PRODUCT_PATH_RE = re.compile(
    r"((?:https?://(?:[a-z0-9-]+\.)?ozon\.(?:ru|by|kz))?/"
    r"(?:product/(?:[^/?#\"'\s<>\\]+-)?\d{5,15}|context/detail/id/\d{5,15})"
    r"(?:/|\b)[^\s\"'<>\\]*)",
    re.IGNORECASE,
)

# Поиск артикула в HTML/JSON (<head>, body, composer-api, deeplink, относительные ссылки /product/...-<sku>)
HEAD_PRODUCT_RE = re.compile(
    r"(?:"
    r"(?:ozon\.(?:ru|by|kz))?/(?:product/(?:[^/\"'?#\s<>\\]*-)?|context/detail/id/)|"
    r"ozon://(?:products|product)/|"
    r"\"(?:sku|skuId|itemId)\"\s*:\s*\"?"
    r")"
    r"(\d{5,15})(?:[/?#\"'\\&\s<>]|$)",
    re.IGNORECASE,
)

# Извлечение артикула из текста поста («Артикул: 12345678», «Арт. 12345678», «Код товара: 12345678»)
TEXT_SKU_RE = re.compile(
    r"(?:артикул|арт\.?|код\s+товара|sku)\s*[:#№\-–—]?\s*(\d{6,15})\b",
    re.IGNORECASE,
)

# Короткие/реферальные пути Ozon (/t/XXXX или /p/oXXXXXXX)
OZON_SHORT_PATH_RE = re.compile(r"^/(?:t|p)/([A-Za-z0-9_-]+)", re.IGNORECASE)

# Заведомо нетоварные разделы (магазины, продавцы, бренды, категории, подборки)
NON_PRODUCT_PATH_RE = re.compile(
    r"^/(?:s|seller|brand|Brands|category|highlight|search|actions|special)/",
    re.IGNORECASE,
)

ANTIBOT_MARKERS = (
    "доступ ограничен",
    "почти готово",
    "проверяем браузер",
    "один момент",
    "incidentid",
    "fab_chlg",
    "fab_chig",
    "ozon-antibot",
    "variti",
)

OUT_OF_STOCK_MARKERS = (
    "этот товар закончился",
    "товара нет в наличии",
    "нет в наличии",
    "товар закончился",
    "раскупили",
    "нет в продаже",
    "недоступен для заказа",
    "товар недоступен",
    "снят с продажи",
    "sold out",
    "out of stock",
    "outofstock",
)

NOT_FOUND_MARKERS = (
    "страница не найдена",
    "товар не найден",
    "такой страницы не существует",
    "ошибка 404",
)

GENERIC_BRAND_NAMES = frozenset(
    {
        "",
        "нет бренда",
        "без бренда",
        "noname",
        "no name",
        "no brand",
        "generic",
        "oem",
        "wildberries",
        "ozon",
    }
)

# Регулярные выражения для очистки заголовка карточки товара от SEO-мусора маркетплейсов
TITLE_SEO_PREFIX_RE = re.compile(
    r"^\s*(?:купить|заказать|покупайте)\s+",
    re.IGNORECASE,
)

TITLE_SEO_SUFFIX_PATTERNS = (
    re.compile(
        r"\s*(?:[—–\-|•/]\s*)?(?:купить|заказать)\s+(?:в\s+интернет[\s-]*магазине|на\s+(?:ozon|озон|wildberries|вайлдберриз|вб)|по\s+(?:выгодной|низкой)\s+цене).*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s+в\s+интернет[\s-]*магазине\s+(?:ozon|озон|wildberries|вайлдберриз).*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s+по\s+(?:выгодной|низкой|доступной)\s+цене.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s+с\s+(?:быстрой\s+)?доставкой\s+(?:по\s+беларуси|в\s+минске|на\s+ozon|на\s+wildberries).*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s*(?:[—–\-|•/]\s*)(?:ozon|озон|wildberries|вайлдберриз|ozon\.by|ozon\.ru|wildberries\.ru|wildberries\.by)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s*\(\s*(?:арт(?:икул)?\.?\s*[:#№]?\s*)?\d{5,15}\s*\)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s+(?:артикул|арт\.?|код\s+товара)\s*[:#№\-–—]?\s*\d{5,15}.*$",
        re.IGNORECASE,
    ),
)

PRICE_VALUE_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9_])"
    r"(\d[\d\s\u00a0\u202f\u2009\u2007]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр\.?|р\.|руб\.?|₽|rub|rur)?"
    r"(?![A-Za-zА-Яа-яЁё0-9_])",
    re.IGNORECASE,
)

# Строгое регулярное выражение цены с обязательным указанием валюты (исключает захват 400/500 из tsBody400Small / HEADLINE_500)
STRICT_PRICE_WITH_CURRENCY_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9_])"
    r"(\d[\d\s\u00a0\u202f\u2009\u2007]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр\.?|р\.|руб\.?|₽|rub|rur)"
    r"(?![A-Za-zА-Яа-яЁё0-9_])",
    re.IGNORECASE,
)

# Поиск подписи средней цены за месяц в Ozon («Средняя за сентябрь», исключая дисклеймер «со средней за прошлый месяц»)
AVG_MONTH_LABEL_RE = re.compile(
    r"(?<!со\s)средн(?:яя|ей)\s+(?:цена\s+)?за\s+"
    r"(?:прошлый\s+месяц|последний\s+месяц|\d+\s*дн(?:ей|я)|месяц|"
    r"январ[ья]|феврал[ья]|март[а]?|апрел[ья]|ма[йя]|июн[ья]|"
    r"июл[ья]|август[а]?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])",
    re.IGNORECASE,
)

OZON_CHEAPER_BLOCK_RE = re.compile(
    r"(?:стало\s+дешевле|цена\s+сейчас|(?<!со\s)средн(?:яя|ей)\s+(?:цена\s+)?за\s+)",
    re.IGNORECASE,
)

# Сумма скидки на бейдже «Стало дешевле» («↓ 4,83 BYN»)
PRICE_DROP_BADGE_RE = re.compile(
    r"[↓⬇]\s*(\d[\d\s\u00a0\u202f\u2009]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр|br|р\.|руб\.?|₽|rub|rur)",
    re.IGNORECASE,
)

JSON_LD_SCRIPT_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

META_TAG_RE = re.compile(
    r"<meta\s+[^>]*?(?:property|name|itemprop)=[\"']([^\"']+)[\"'][^>]*?content=[\"']([^\"']*)[\"'][^>]*?>|"
    r"<meta\s+[^>]*?content=[\"']([^\"']*)[\"'][^>]*?(?:property|name|itemprop)=[\"']([^\"']+)[\"'][^>]*?>",
    re.IGNORECASE | re.DOTALL,
)

H1_TAG_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)






__all__ = [name for name in globals() if not name.startswith("__")]
