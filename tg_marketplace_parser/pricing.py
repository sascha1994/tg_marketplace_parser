from contextlib import suppress
import json
import re
from typing import Any
from urllib.parse import (
    parse_qs,
    unquote,
    urlparse,
)
from .common import clean_html_tags
from .constants import (
    AVG_MONTH_LABEL_RE,
    OZON_CHEAPER_BLOCK_RE,
    PRICE_DROP_BADGE_RE,
    PRICE_VALUE_RE,
    STRICT_PRICE_WITH_CURRENCY_RE,
)

_IGNORED_OZON_STYLE_KEYS = frozenset(
    {"textStyle", "style", "color", "icon", "iconName", "iconColor", "className"}
)


def format_byn_price(price_byn: float | None) -> str | None:
    """Форматирует цену в белорусских рублях (например: 24,50 бел. руб. или 120 бел. руб.)."""
    if price_byn is None or price_byn <= 0:
        return None
    rounded = round(float(price_byn), 2)
    int_part = int(rounded)
    frac_part = int(round((rounded - int_part) * 100))
    int_str = f"{int_part:,}".replace(",", " ")
    if frac_part == 0:
        return f"{int_str} бел. руб."
    return f"{int_str},{frac_part:02d} бел. руб."

def parse_price_value_and_currency(
    raw_value: Any,
    default_currency: str = "BYN",
    *,
    require_currency: bool = False,
) -> tuple[float | None, str]:
    """Извлекает числовое значение цены и код валюты ('BYN' или 'RUB') из строки или числа.

    Если require_currency=True, требует явного присутствия обозначения валюты рядом с числом
    (чтобы не принимать технические числа вроде tsBody400Small / 400 за цену).
    """
    if raw_value is None:
        return None, default_currency.upper()

    if isinstance(raw_value, (int, float)):
        if require_currency:
            return None, default_currency.upper()
        val = float(raw_value)
        return (val if val > 0 else None), default_currency.upper()

    raw_str = str(raw_value)
    if "\\u" in raw_str:
        for esc in ("\\u00a0", "\\u202f", "\\u2009", "\\u2007", "\\u00A0", "\\u202F", "\\u2009"):
            raw_str = raw_str.replace(esc, " ")
    text = clean_html_tags(raw_str)
    if not text:
        return None, default_currency.upper()

    pattern = STRICT_PRICE_WITH_CURRENCY_RE if require_currency else PRICE_VALUE_RE
    match = pattern.search(text)
    if not match:
        return None, default_currency.upper()

    num_str = re.sub(r"[\s\u00a0\u202f\u2009\u2007]+", "", match.group(1)).replace(",", ".")
    try:
        val = float(num_str)
    except ValueError:
        return None, default_currency.upper()

    if val <= 0:
        return None, default_currency.upper()

    detected_currency = default_currency.upper()
    unit = (match.group(2) or "").casefold()
    if unit:
        if any(m in unit for m in ("byn", "бел", "бр")):
            detected_currency = "BYN"
        elif any(m in unit for m in ("₽", "rub", "rur", "руб", "р.")):
            detected_currency = "RUB"
    elif not require_currency:
        text_lower = text.casefold()
        if any(marker in text_lower for marker in ("byn", "бел", "бр")):
            detected_currency = "BYN"
        elif any(marker in text_lower for marker in ("₽", "rub", "rur", "рос")):
            detected_currency = "RUB"

    return val, detected_currency

def convert_to_byn(
    amount: float | None,
    currency: str,
    rub_to_byn_rate: float,
) -> float | None:
    """Конвертирует сумму в белорусские рубли (BYN), если исходная валюта — RUB."""
    if amount is None or amount <= 0:
        return None
    curr = (currency or "BYN").upper()
    if curr == "BYN":
        return round(amount, 2)
    if curr in {"RUB", "RUR"}:
        return round(amount * rub_to_byn_rate, 2)
    return round(amount, 2)

def _is_plausible_avg_price(
    avg_byn: float | None,
    actual_price_byn: float | None,
    old_price_byn: float | None = None,
) -> bool:
    """Проверяет, что найденная средняя цена реалистична относительно актуальной цены товара."""
    if avg_byn is None or avg_byn <= 0:
        return False
    if actual_price_byn is not None and actual_price_byn > 0:
        if avg_byn <= actual_price_byn * 1.002:
            return False
        if avg_byn > max(actual_price_byn * 6.0, actual_price_byn + 250.0):
            return False
    if old_price_byn is not None and old_price_byn > 0:
        if abs(avg_byn - old_price_byn) < 0.01:
            return False
    return True

def _extract_all_strict_prices_byn(
    text: str,
    *,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> list[float]:
    """Извлекает все цены с явно указанной валютой из текста и конвертирует их в BYN."""
    results: list[float] = []
    for m in STRICT_PRICE_WITH_CURRENCY_RE.finditer(text):
        raw_p = f"{m.group(1)} {m.group(2)}"
        val, curr = parse_price_value_and_currency(raw_p, default_currency=default_currency, require_currency=True)
        byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
        if byn_val is not None and byn_val > 0:
            results.append(byn_val)
    return results

def _find_math_trio_avg_price(
    prices_byn: list[float],
    actual_price_byn: float | None = None,
    old_price_byn: float | None = None,
) -> float | None:
    """Находит среднюю цену в блоке «Стало дешевле» по точной формуле Ozon:
    Цена сейчас (actual) + Скидка на бейдже (drop) == Средняя за месяц (avg).
    Например: 29,15 + 8,28 == 37,43 BYN или 3,52 + 0,24 == 3,76 BYN.
    """
    if len(prices_byn) < 2:
        return None

    # 1. Если известна актуальная цена (например, 29.15 BYN), ищем пару (drop, avg), где actual + drop == avg
    if actual_price_byn is not None and actual_price_byn > 0:
        for i, drop_val in enumerate(prices_byn):
            if drop_val <= 0:
                continue
            expected_avg = round(actual_price_byn + drop_val, 2)
            for j, cand_avg in enumerate(prices_byn):
                if i == j:
                    continue
                if abs(cand_avg - expected_avg) <= 0.05 and _is_plausible_avg_price(
                    cand_avg, actual_price_byn, old_price_byn
                ):
                    return cand_avg

    # 2. Если в списке 3 и более цен (actual, drop, avg), проверяем тройку p_now + p_drop == p_avg
    if len(prices_byn) >= 3:
        for i in range(len(prices_byn)):
            p_now = prices_byn[i]
            if actual_price_byn is not None and abs(p_now - actual_price_byn) > 0.05:
                continue
            for j in range(len(prices_byn)):
                if j == i:
                    continue
                p_drop = prices_byn[j]
                if p_drop <= 0:
                    continue
                for k in range(len(prices_byn)):
                    if k in (i, j):
                        continue
                    p_avg = prices_byn[k]
                    if p_avg > p_now * 1.002 and abs((p_now + p_drop) - p_avg) <= 0.05:
                        if _is_plausible_avg_price(p_avg, actual_price_byn or p_now, old_price_byn):
                            return p_avg

    return None

def extract_avg_month_price_from_text(
    text: str,
    *,
    actual_price_byn: float | None = None,
    old_price_byn: float | None = None,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> float | None:
    """Ищет среднюю цену за месяц в блоке «Стало дешевле» (например: «37,43 BYN Средняя за сентябрь»)."""
    if not text:
        return None

    raw_str = text
    if "\\u" in raw_str:
        for esc in ("\\u00a0", "\\u202f", "\\u2009", "\\u2007", "\\u00A0", "\\u202F"):
            raw_str = raw_str.replace(esc, " ")
    if "<script" in raw_str.lower() or "<style" in raw_str.lower():
        raw_str = re.sub(r"<script[^>]*>.*?</script>", " ", raw_str, flags=re.IGNORECASE | re.DOTALL)
        raw_str = re.sub(r"<style[^>]*>.*?</style>", " ", raw_str, flags=re.IGNORECASE | re.DOTALL)

    clean_text = clean_html_tags(raw_str)
    if not clean_text:
        return None

    # 1. Ищем подпись «Средняя за <месяц>» и проверяем как математическую тройку, так и ближайшую цену с валютой
    for label_match in AVG_MONTH_LABEL_RE.finditer(clean_text):
        label_start, label_end = label_match.span()
        context_window = clean_text[max(0, label_start - 650):min(len(clean_text), label_end + 350)]
        trio_avg = _find_math_trio_avg_price(
            _extract_all_strict_prices_byn(
                context_window,
                default_currency=default_currency,
                rub_to_byn_rate=rub_to_byn_rate,
            ),
            actual_price_byn=actual_price_byn,
            old_price_byn=old_price_byn,
        )
        if trio_avg is not None:
            return trio_avg

        # Проверяем окно до 650 символов ПЕРЕД подписью «Средняя за <месяц>» (от ближайшей цены к дальней)
        prefix_window = clean_text[max(0, label_start - 650):label_start]
        prefix_matches = list(STRICT_PRICE_WITH_CURRENCY_RE.finditer(prefix_window))
        for m_price in reversed(prefix_matches):
            raw_p = f"{m_price.group(1)} {m_price.group(2)}"
            val, curr = parse_price_value_and_currency(raw_p, default_currency=default_currency, require_currency=True)
            byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
            if _is_plausible_avg_price(byn_val, actual_price_byn, old_price_byn):
                return byn_val

        # Проверяем окно до 350 символов ПОСЛЕ подписи («Средняя за сентябрь: 37,43 BYN»)
        suffix_window = clean_text[label_end:label_end + 350]
        for m_after in STRICT_PRICE_WITH_CURRENCY_RE.finditer(suffix_window):
            raw_p = f"{m_after.group(1)} {m_after.group(2)}"
            val, curr = parse_price_value_and_currency(raw_p, default_currency=default_currency, require_currency=True)
            byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
            if _is_plausible_avg_price(byn_val, actual_price_byn, old_price_byn):
                return byn_val

    # 2. Проверяем математическую тройку (Цена сейчас + Скидка = Средняя) рядом с «Стало дешевле» / «Цена сейчас»
    for block_match in OZON_CHEAPER_BLOCK_RE.finditer(clean_text):
        b_start, b_end = block_match.span()
        window = clean_text[max(0, b_start - 400):min(len(clean_text), b_end + 700)]
        trio_avg = _find_math_trio_avg_price(
            _extract_all_strict_prices_byn(
                window,
                default_currency=default_currency,
                rub_to_byn_rate=rub_to_byn_rate,
            ),
            actual_price_byn=actual_price_byn,
            old_price_byn=old_price_byn,
        )
        if trio_avg is not None:
            return trio_avg

    # 3. Если есть актуальная цена и зелёный бейдж снижения со стрелкой «↓ 8,28 BYN»
    if actual_price_byn is not None and actual_price_byn > 0:
        m_drop = PRICE_DROP_BADGE_RE.search(clean_text)
        if m_drop:
            raw_drop = f"{m_drop.group(1)} {m_drop.group(2)}"
            drop_val, drop_curr = parse_price_value_and_currency(
                raw_drop,
                default_currency=default_currency,
                require_currency=True,
            )
            drop_byn = convert_to_byn(drop_val, drop_curr, rub_to_byn_rate)
            if drop_byn is not None and drop_byn > 0:
                candidate_avg = round(actual_price_byn + drop_byn, 2)
                if _is_plausible_avg_price(candidate_avg, actual_price_byn, old_price_byn):
                    return candidate_avg

    return None

def _collect_clean_leaf_strings(node: Any, out: list[str] | None = None) -> list[str]:
    """Собирает все текстовые строки из JSON-дерева виджета Ozon, отбрасывая технические поля стилей (tsBody400Small и т.д.)."""
    if out is None:
        out = []
    if isinstance(node, str):
        s = node.strip()
        if s and not s.startswith(("tsBody", "tsHeadline", "tsCompact", "ic_", "http://", "https://")):
            out.append(s)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in _IGNORED_OZON_STYLE_KEYS:
                continue
            _collect_clean_leaf_strings(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_clean_leaf_strings(item, out)
    return out

def extract_ozon_avg_price_from_state(
    state: Any,
    *,
    actual_price_byn: float | None = None,
    old_price_byn: float | None = None,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> float | None:
    """Рекурсивно (снизу вверх по дереву JSON) ищет среднюю цену за месяц в состоянии виджетов Ozon."""
    if isinstance(state, dict):
        # 1. Прямые ключи средней цены, если присутствуют
        for key in ("averagePrice", "avgPrice", "monthAvgPrice", "averageMonthPrice", "prevMonthAvgPrice"):
            if state.get(key):
                val, curr = parse_price_value_and_currency(
                    state[key],
                    default_currency=default_currency,
                    require_currency=False,
                )
                byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
                if _is_plausible_avg_price(byn_val, actual_price_byn, old_price_byn):
                    return byn_val

        # 2. Сначала рекурсивно проверяем дочерние узлы (поиск наименьшего общего предка LCA для «37,43 BYN» и «Средняя за ...»)
        for k, v in state.items():
            if k in _IGNORED_OZON_STYLE_KEYS:
                continue
            if isinstance(v, (dict, list)):
                found = extract_ozon_avg_price_from_state(
                    v,
                    actual_price_byn=actual_price_byn,
                    old_price_byn=old_price_byn,
                    default_currency=default_currency,
                    rub_to_byn_rate=rub_to_byn_rate,
                )
                if found is not None:
                    return found

        # 3. Проверяем очищенные листовые строки текущего поддерева (без tsBody400Small / tsHeadline500Medium)
        leaves = _collect_clean_leaf_strings(state)
        if leaves:
            joined_pipe = " | ".join(leaves)
            if OZON_CHEAPER_BLOCK_RE.search(joined_pipe):
                found_in_leaves = extract_avg_month_price_from_text(
                    joined_pipe,
                    actual_price_byn=actual_price_byn,
                    old_price_byn=old_price_byn,
                    default_currency=default_currency,
                    rub_to_byn_rate=rub_to_byn_rate,
                )
                if found_in_leaves is not None:
                    return found_in_leaves
                # На случай если число и валюта лежали в соседних листовых строках ("37,43" и "BYN")
                joined_space = " ".join(leaves)
                found_in_space = extract_avg_month_price_from_text(
                    joined_space,
                    actual_price_byn=actual_price_byn,
                    old_price_byn=old_price_byn,
                    default_currency=default_currency,
                    rub_to_byn_rate=rub_to_byn_rate,
                )
                if found_in_space is not None:
                    return found_in_space

    elif isinstance(state, list):
        for item in state:
            if isinstance(item, (dict, list)):
                found = extract_ozon_avg_price_from_state(
                    item,
                    actual_price_byn=actual_price_byn,
                    old_price_byn=old_price_byn,
                    default_currency=default_currency,
                    rub_to_byn_rate=rub_to_byn_rate,
                )
                if found is not None:
                    return found

        leaves = _collect_clean_leaf_strings(state)
        if leaves:
            joined_pipe = " | ".join(leaves)
            if OZON_CHEAPER_BLOCK_RE.search(joined_pipe):
                found_in_leaves = extract_avg_month_price_from_text(
                    joined_pipe,
                    actual_price_byn=actual_price_byn,
                    old_price_byn=old_price_byn,
                    default_currency=default_currency,
                    rub_to_byn_rate=rub_to_byn_rate,
                )
                if found_in_leaves is not None:
                    return found_in_leaves

    return None

def normalize_ozon_modal_path(raw_link: str) -> str | None:
    """Преобразует ссылку или deeplink модального окна Ozon («Стало дешевле») в относительный путь для API."""
    if not raw_link or not isinstance(raw_link, str):
        return None
    s = raw_link.strip().replace("\\/", "/")
    if not s or len(s) > 1500:
        return None
    s_lower = s.lower()
    if any(s_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".svg", ".gif", ".css", ".js", ".woff", ".woff2")):
        return None
    if s_lower.startswith("ozon://"):
        s = "/" + s[len("ozon://"):].lstrip("/")
    elif s_lower.startswith(("http://", "https://")):
        try:
            parsed = urlparse(s)
        except ValueError:
            return None
        s = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    if not s.startswith("/"):
        return None
    s_lower = s.lower()
    if "/page/json/v2" in s_lower and "url=" in s_lower:
        with suppress(Exception):
            qs = parse_qs(urlparse(s).query)
            inner = unquote(qs.get("url", [""])[0])
            if inner.startswith("/"):
                return inner
    if any(
        k in s_lower
        for k in (
            "/modal/",
            "modal=",
            "pricedecrease",
            "price_decrease",
            "price-decrease",
            "pricehistory",
            "price_history",
            "cheaper",
            "stat=price",
        )
    ):
        return s
    return None

def extract_ozon_price_modal_urls(payload: dict[str, Any]) -> list[str]:
    """Извлекает пути модальных окон «Стало дешевле» из widgetStates ответа Ozon."""
    if not isinstance(payload, dict):
        return []

    found_paths: list[str] = []

    def _add_candidate(raw_val: Any, *, force_if_path: bool = False) -> None:
        if not isinstance(raw_val, str):
            return
        norm = normalize_ozon_modal_path(raw_val)
        if norm and norm not in found_paths:
            found_paths.append(norm)
            return
        if force_if_path:
            s = raw_val.strip().replace("\\/", "/")
            if s.startswith("ozon://"):
                s = "/" + s[len("ozon://"):].lstrip("/")
            elif s.startswith(("http://", "https://")):
                with suppress(ValueError):
                    p = urlparse(s)
                    s = (p.path or "/") + (f"?{p.query}" if p.query else "")
            if (
                s.startswith("/")
                and not s.startswith(("/product/", "/brand/", "/category/", "/seller/", "/s/", "/search/"))
                and not any(s.lower().endswith(ext) for ext in (".jpg", ".png", ".webp", ".svg", ".gif"))
                and s not in found_paths
            ):
                found_paths.append(s)

    def _walk_node(node: Any, in_cheaper_context: bool = False) -> None:
        if isinstance(node, dict):
            node_dump_lower = json.dumps(node, ensure_ascii=False).lower()
            has_cheaper = in_cheaper_context or ("стало дешевле" in node_dump_lower) or ("pricedecrease" in node_dump_lower)
            # Если этот узел небольшой и непосредственно описывает кнопку «Стало дешевле», берём любые его ссылки
            is_tight_cheaper = ("стало дешевле" in node_dump_lower) and len(node_dump_lower) < 1200
            for k, v in node.items():
                k_lower = str(k).lower()
                if isinstance(v, str):
                    if k_lower in ("link", "deeplink", "url", "modalurl", "uri", "href", "action", "path", "targeturl", "contenturl"):
                        _add_candidate(v, force_if_path=is_tight_cheaper)
                    elif has_cheaper:
                        _add_candidate(v, force_if_path=False)
                elif isinstance(v, (dict, list)):
                    _walk_node(v, has_cheaper)
        elif isinstance(node, list):
            for item in node:
                _walk_node(item, in_cheaper_context)

    widget_states = payload.get("widgetStates")
    if isinstance(widget_states, dict):
        for w_key, raw_state in widget_states.items():
            if not isinstance(raw_state, str) or not raw_state.strip():
                continue
            w_lower = w_key.lower()
            raw_lower = raw_state.lower()
            if "webprice" in w_lower or "pricedecrease" in w_lower or "стало дешевле" in raw_lower or "modal" in raw_lower:
                with suppress(Exception):
                    parsed_st = json.loads(raw_state)
                    _walk_node(parsed_st, "pricedecrease" in w_lower or "стало дешевле" in raw_lower)

    return found_paths
