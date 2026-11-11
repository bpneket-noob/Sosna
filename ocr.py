"""Распознавание текста через Windows OCR (WinRT): строки, блоки, автоязык, фильтр мусора.

Оптимизации: постоянный event-loop поток для WinRT (вместо нового loop и
полного picklify-обхода графа объектов на каждый вызов), умный автоязык
(сначала алфавит, потом максимум один уточняющий проход — вместо
паса на каждый установленный язык), переиспользование mss-инстанса,
очистка OCR-текста от дублей и мусора до перевода.
"""
import asyncio
import re
import threading

from PIL import Image, ImageFilter

import sosna_log

log = sosna_log.get()

# Целевая ширина мелких фрагментов: Windows OCR уверенно читает текст
# от ~15 px высоты, поэтому маленькие выделения увеличиваем перед распознаванием.
_TARGET_MIN_WIDTH = 1600
_MAX_SCALE = 4
_MAX_SIDE = 8000  # лимит Windows OCR с запасом

# Соответствие языка OCR и кода источника NLLB
OCR_TO_NLLB = {
    "en-US": "eng_Latn", "en": "eng_Latn",
    "ru": "rus_Cyrl", "ru-RU": "rus_Cyrl",
    "zh-CN": "zho_Hans", "zh-Hans": "zho_Hans", "zh": "zho_Hans",
    "uk": "ukr_Cyrl",
    "de": "deu_Latn", "fr": "fra_Latn", "es": "spa_Latn",
    "it": "ita_Latn", "pt": "por_Latn", "pl": "pol_Latn",
    "tr": "tur_Latn", "ja": "jpn_Jpan", "ko": "kor_Hang",
}

# --------- постоянный event-loop для WinRT OCR ---------

_ocr_loop = None
_loop_lock = threading.Lock()


def _ensure_ocr_loop():
    """Фоновый поток с event loop: все вызовы WinRT идут через него."""
    global _ocr_loop
    with _loop_lock:
        if _ocr_loop is None or _ocr_loop.is_closed():
            _ocr_loop = asyncio.new_event_loop()
            threading.Thread(target=_ocr_loop.run_forever,
                             name="sosna-ocr-loop", daemon=True).start()
    return _ocr_loop


async def _extract_lines_coro(img: Image.Image, lang: str) -> list:
    """Распознать и извлечь строки в чистые dict (WinRT-объекты не покидают
    поток loop'а — никакого дорогого picklify-обхода всего графа)."""
    import winocr
    result = await winocr.recognize_pil(img, lang)
    lines = []
    for ln in result.lines:
        words = []
        for w in ln.words:
            r = w.bounding_rect
            words.append((r.x, r.y, r.width, r.height))
        lines.append({"text": (ln.text or "").strip(), "words": words})
    return lines


def _ocr_lines(img: Image.Image, lang: str) -> list:
    """Синхронная обёртка: строки [{'text', 'words': [(x,y,w,h)...]}]."""
    loop = _ensure_ocr_loop()
    fut = asyncio.run_coroutine_threadsafe(_extract_lines_coro(img, lang), loop)
    try:
        return fut.result(timeout=60)
    except Exception as e:
        log.error("OCR (%s) не удался: %s", lang, e)
        raise


# --------- скриншоты: mss переиспользуется в рамках потока ---------

_tls = threading.local()


def _mss():
    sct = getattr(_tls, "sct", None)
    if sct is None:
        import mss
        sct = _tls.sct = mss.mss()
    return sct


def grab_display(monitor: dict) -> Image.Image:
    """Снять монитор/область через потоковый mss-инстанс (BGRA → RGB)."""
    shot = _mss().grab(monitor)
    return Image.frombytes("RGB", shot.size, shot.rgb)


def available_languages():
    """Список установленных языков распознавания, например ['en-US', 'ru']."""
    from winrt.windows.media.ocr import OcrEngine
    return [l.language_tag for l in OcrEngine.available_recognizer_languages]


def _prepare(img: Image.Image) -> Image.Image:
    """Увеличить мелкие изображения (LANCZOS + лёгкая резкость) для OCR."""
    w, h = img.size
    scale = min(_MAX_SCALE, max(1, round(_TARGET_MIN_WIDTH / max(w, 1))))
    if scale > 1:
        img = img.resize((w * scale, h * scale), Image.LANCZOS)
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=110, threshold=2))
    m = max(img.size)
    if m > _MAX_SIDE:  # страховка для огромных выделений
        k = _MAX_SIDE / m
        img = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
    return img


# --------- очистка OCR-текста (до перевода) ---------

_JUNK_LINE = re.compile(r"^[\s|_\-~`'\"«»·•.,;:!?¿¡(){}\[\]<>\\/@#$%^&*+=]*$")


def clean_text(text: str) -> str:
    """Убрать типовой OCR-мусор: дубли строк подряд, лишние пробелы,
    строки из символов-заглушек. Меньше мусора — быстрее и точнее перевод."""
    if not text:
        return text
    out, prev = [], None
    for raw in text.splitlines():
        ln = re.sub(r"[ \t]+", " ", raw).strip()
        if not ln or ln == prev:
            continue  # пустые и подряд задвоенные строки
        if len(ln) <= 4 and _JUNK_LINE.match(ln):
            continue  # «|», «___», «·.·» и прочая шелуха от иконок
        out.append(ln)
        prev = ln
    return "\n".join(out)


# --------- группировка в блоки ---------

def _same_block(a, b) -> bool:
    """Смежные строки одного абзаца: умеренный вертикальный зазор + перекрытие."""
    gap = b["y"] - (a["y"] + a["h"])
    avg_h = (a["h"] + b["h"]) / 2
    if gap > avg_h * 1.0:  # межабзацный отступ заметно больше межстрочного
        return False
    overlap = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    return overlap > 0.25 * min(a["w"], b["w"])


def _group_lines(lines: list) -> list:
    """Сгруппировать строки в абзацы (блоки) по близости."""
    lines = sorted(lines, key=lambda l: (l["y"], l["x"]))
    groups = []
    cur = []
    for ln in lines:
        if cur and not _same_block(cur[-1], ln):
            groups.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        groups.append(cur)
    blocks = []
    for g in groups:
        x1 = min(l["x"] for l in g)
        y1 = min(l["y"] for l in g)
        x2 = max(l["x"] + l["w"] for l in g)
        y2 = max(l["y"] + l["h"] for l in g)
        blocks.append({
            "text": " ".join(l["text"] for l in g),
            "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1,
            # средняя высота строки исходника — по ней полноэкранный оверлей
            # подбирает кегль перевода примерно как у оригинала
            "src_h": sum(l["h"] for l in g) / len(g),
        })
    return blocks


# --------- фильтр мусора ---------

def _token_wordlike(tok: str, base: str) -> bool:
    """Похоже ли слово на настоящее слово (а не OCR-мусор)."""
    letters = [c for c in tok if c.isalpha()]
    if not letters or any(c.isdigit() for c in tok):
        return False
    cyr = sum(1 for c in tok if "\u0400" <= c <= "\u04FF")
    lat = sum(1 for c in tok if c.isascii() and c.isalpha())
    if cyr and lat:            # смесь алфавитов внутри слова («СbeUJb») — мусор
        return False
    if base in ("en", "de", "fr", "es", "it", "pt", "pl", "tr") and cyr:
        return False           # латинский язык, а слово кириллицей — транслит-каша
    if base == "ru" and lat:
        return False
    if lat and not any(c in "aeiouy" for c in tok.lower()):
        return False           # латиница без гласных («MffKVlX»)
    if cyr and not any(c in "аеёиоуыэюя" for c in tok.lower()):
        return False           # кириллица без гласных («ВЗПРХ»)
    return True


def is_garbage(text: str, lang_tag: str = "en-US") -> bool:
    """Мусорный ли распознанный текст.

    True — блок лучше вообще не переводить: иконки, цифро-буквенная каша
    («06LUaiC9»), транслитерация чужим алфавитом, слова без гласных.
    """
    base = (lang_tag or "en").split("-")[0].lower()
    alnum = [c for c in text if c.isalnum()]
    if len(alnum) < 3:
        return True
    tokens = re.findall(r"[^\W_]+", text, re.UNICODE)
    letter_tokens = [t for t in tokens if any(c.isalpha() for c in t)]
    if not letter_tokens:
        return True
    # «гибридные» токены (буквы вперемешку с цифрами: 06LUaiC9, B03MOXHO) —
    # OCR-артефакты; в связном тексте их почти не бывает
    digit_mixed = [t for t in tokens
                   if any(c.isdigit() for c in t) and any(c.isalpha() for c in t)]
    if len(digit_mixed) >= 2:
        return True
    good = sum(1 for t in letter_tokens if _token_wordlike(t, base))
    if good / len(letter_tokens) < 0.5:
        return True
    # «макетные» строки из оверлеев игр (клавиши QWERTY: «RSDFC z xcvq»):
    # 2+ латинских токена длиной 4+ без единой гласной — настоящие слова
    # без гласных почти не встречаются, а такие куски переводятся в кашу
    no_vowel = [t for t in letter_tokens
                if len(t) >= 4
                and all(ch.isascii() and ch.isalpha() for ch in t)
                and not any(ch in "aeiouy" for ch in t.lower())]
    if len(no_vowel) >= 2:
        return True
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
    lat = sum(1 for c in text if c.isascii() and c.isalpha())
    letters = cyr + lat
    if letters:
        # доминирование чужого алфавита (ru-распознаватель выдаёт латиницу и наоборот)
        foreign = lat / letters if base == "ru" else cyr / letters
        if foreign > 0.45:
            return True
    if len(alnum) >= 5:
        low = text.lower()
        if lat >= cyr and lat > 0 and not any(v in low for v in "aeiouy"):
            return True
        if cyr > lat and cyr > 0 and not any(v in low for v in "аеёиоуыэюя"):
            return True
    return False


# --------- фильтры контента (полноэкранный перевод) ---------

# целевой язык NLLB -> письменность
TGT_SCRIPT = {"rus_Cyrl": "cyr", "ukr_Cyrl": "cyr", "eng_Latn": "lat"}

_DOMAINISH = re.compile(r"^[\w\-]+(\.[\w\-]+)+([/?#][^\s]*)?$", re.UNICODE)


def dominant_script(text: str) -> str | None:
    """Какая письменность доминирует в тексте: cyr | lat | han | kana | hang."""
    counts = {"cyr": 0, "lat": 0, "han": 0, "kana": 0, "hang": 0}
    for c in text:
        o = ord(c)
        if 0x0400 <= o <= 0x04FF:
            counts["cyr"] += 1
        elif c.isascii() and c.isalpha():
            counts["lat"] += 1
        elif 0x4E00 <= o <= 0x9FFF:
            counts["han"] += 1
        elif 0x3040 <= o <= 0x30FF:
            counts["kana"] += 1
        elif 0xAC00 <= o <= 0xD7AF:
            counts["hang"] += 1
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] >= 2 else None


def looks_like_link(text: str) -> bool:
    """Ссылка/домен: переводить бессмысленно."""
    t = text.lower()
    if "://" in t or "www." in t or t.startswith("mailto:"):
        return True
    tokens = text.split()
    if not tokens:
        return False
    domainish = sum(1 for tok in tokens if _DOMAINISH.match(tok))
    return domainish / len(tokens) >= 0.5


def looks_like_noise(text: str) -> bool:
    """Символы/цифры/коды без настоящих слов: «| > _», «12:45», «AB-1234»."""
    alpha = [c for c in text if c.isalpha()]
    if len(alpha) < 2:
        return True
    stripped = text.strip()
    if len(stripped) >= 3 and stripped == stripped[0] * len(stripped):
        return True  # ----, ....., AAAAA
    # серийники/таймкоды/версии: все группы — цифры или короткий КАПС
    # («AB-1234-CD», «v2.0 REL 2024») — связной речью не являются
    toks = [t for t in re.split(r"[\s\-_./:,;|#]+", stripped) if t]
    if len(toks) >= 3:
        codeish = sum(1 for t in toks
                      if t.isdigit() or (t.isalpha() and t.isupper() and len(t) <= 4))
        has_digit = any(t.isdigit() for t in toks)
        if has_digit and codeish == len(toks):
            return True
    return False


def should_translate_block(text: str, ocr_lang: str, tgt_nllb: str) -> bool:
    """Нужен ли блоку перевода (полноэкранный режим).

    Отсекаем: ссылки, символьно-цифровой мусор, OCR-белиберду и текст,
    УЖЕ написанный на целевом языке (иначе «русский на русский» рисует
    плашку-дубль поверх оригинала).
    """
    text = (text or "").strip()
    if len(text) < 2:
        return False
    if looks_like_link(text) or looks_like_noise(text):
        return False
    if is_garbage(text, ocr_lang):
        return False
    tgt_script = TGT_SCRIPT.get(tgt_nllb)
    if tgt_script and dominant_script(text) == tgt_script:
        return False  # уже на целевом языке
    return True


# --------- публичный API ---------

def recognize(pil_img: Image.Image, lang: str) -> str:
    """Распознать текст на изображении. Возвращает очищенный текст с переводами строк."""
    with sosna_log.stage(f"ocr:{lang}", log=log):
        lines = _ocr_lines(_prepare(pil_img), lang)
    return clean_text("\n".join(l["text"] for l in lines if l["text"])).strip()


def recognize_blocks(pil_img: Image.Image, lang: str) -> list:
    """Распознать текст блоками-абзацами с координатами (в пикселях исходной картинки)."""
    with sosna_log.stage(f"ocr-blocks:{lang}", log=log):
        prep = _prepare(pil_img)
        scale = prep.width / max(pil_img.width, 1)
        raw = _ocr_lines(prep, lang)
    lines = []
    for ln in raw:
        if not ln["text"] or not ln["words"]:
            continue
        xs = [w[0] for w in ln["words"]]
        ys = [w[1] for w in ln["words"]]
        x2 = max(w[0] + w[2] for w in ln["words"])
        y2 = max(w[1] + w[3] for w in ln["words"])
        lines.append({"text": ln["text"], "x": min(xs), "y": min(ys),
                      "w": x2 - min(xs), "h": y2 - min(ys)})
    out = []
    for b in _group_lines(lines):
        alnum = [c for c in b["text"] if c.isalnum()]
        if len(alnum) <= 1:
            continue  # одиночные «буквы» от иконок — мусор, лучше ничего
        if looks_like_link(b["text"]) or looks_like_noise(b["text"]):
            continue  # ссылки и символьно-цифровые блоки не переводим
        if is_garbage(b["text"], lang):
            continue  # белиберда не переводится вовсе
        out.append({
            "text": clean_text(b["text"]),
            "x": b["x"] / scale, "y": b["y"] / scale,
            "w": b["w"] / scale, "h": b["h"] / scale,
            "src_h": b["src_h"] / scale,
        })
    return out


def _score(text: str, lang: str) -> float:
    """Оценка «родства» текста и языка распознавания (чем больше — тем лучше)."""
    base = lang.split("-")[0].lower()
    if base.startswith("ru"):
        native = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
    elif base.startswith(("en", "de", "fr", "es", "it", "pt", "pl", "tr")):
        native = sum(1 for c in text if c.isascii() and c.isalpha())
    else:
        native = sum(1 for c in text if c.isalpha())
    junky = sum(
        1 for t in text.split()
        if any(ch.isdigit() for ch in t) and any(ch.isalpha() for ch in t)
    )
    return 2 * native - 8 * junky + 0.1 * len(text)


def _alphabet_suggests(text: str, langs: list) -> str | None:
    """Какой установленный язык лучше подходит тексту по алфавиту."""
    han = sum(1 for c in text if "\u4E00" <= c <= "\u9FFF")
    kana = sum(1 for c in text if "\u3040" <= c <= "\u30FF")
    hang = sum(1 for c in text if "\uAC00" <= c <= "\uD7AF")
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
    bases = {l.split("-")[0].lower() for l in langs}
    if kana >= 2 and "ja" in bases:
        return next(l for l in langs if l.split("-")[0].lower() == "ja")
    if hang >= 2 and "ko" in bases:
        return next(l for l in langs if l.split("-")[0].lower() == "ko")
    if han >= 2 and "zh" in bases:
        return next(l for l in langs if l.split("-")[0].lower() == "zh")
    if cyr >= 2:
        if "ru" in bases:
            return next(l for l in langs if l.split("-")[0].lower() == "ru")
        if "uk" in bases:
            return next(l for l in langs if l.split("-")[0].lower() == "uk")
    return None


def detect_lang(pil_img: Image.Image, langs: list | None = None) -> tuple:
    """Автоопределение языка: 1–3 прохода вместо полного перебора.

    Раньше — по полному проходу OCR на каждый установленный язык.
    Теперь кандидаты: базовый язык (en-US — самый «нейтральный»), «ru»
    (кириллица под en-распознавателем выглядит латинским мусором, алфавит
    текста её не выдаёт — нужен отдельный проход) и язык по алфавиту
    результата (кана/хангыль/хань). Побеждает лучший по оценке.
    Возвращает (lang_tag, text).
    """
    langs = langs or available_languages()
    if not langs:
        return ("en-US", "")
    prep = _prepare(pil_img)

    def first_choice():
        if "en-US" in langs:
            return "en-US"  # латиница — самый «нейтральный» стартер
        for l in langs:
            if l.split("-")[0].lower() in ("en", "de", "fr", "es", "it",
                                           "pt", "pl", "tr"):
                return l
        return langs[0]

    with sosna_log.stage("ocr-detect", log=log):
        lang1 = first_choice()
        candidates = [lang1]
        # кириллицу en-распознаватель не выдаёт кириллицей — пробуем ru явно
        for cand in langs:
            if cand.split("-")[0].lower() in ("ru", "uk") and cand not in candidates:
                candidates.append(cand)
                break

        results = []
        for lang in candidates:
            text = "\n".join(
                l["text"] for l in _ocr_lines(prep, lang) if l["text"]).strip()
            if text:
                results.append((lang, text, _score(text, lang)))

        if not results:
            # кандидаты не увидели текста — перебираем остальные до первого
            # непустого результата (защита от экзотических языков)
            for lang in langs:
                if lang in candidates:
                    continue
                t = "\n".join(
                    l["text"] for l in _ocr_lines(prep, lang) if l["text"]).strip()
                if t:
                    results.append((lang, t, _score(t, lang)))
                    break

        if not results:
            return (lang1, "")
        best_lang, best_text, _score_v = max(results, key=lambda r: r[2])
        # алфавит результата ясно указывает на другой установленный язык —
        # один уточняющий проход (кана/хань/хангыль: en-проход их портит)
        hint = _alphabet_suggests(best_text, langs)
        if hint and hint not in candidates:
            t2 = "\n".join(
                l["text"] for l in _ocr_lines(prep, hint) if l["text"]).strip()
            if t2 and _score(t2, hint) > _score_v:
                best_lang, best_text = hint, t2

    return (best_lang, clean_text(best_text).strip())


def nllb_src_for(lang_tag: str, text: str, fallback: str = "eng_Latn") -> str:
    """Код исходного языка NLLB: по тегу OCR, если он неизвестен — по алфавиту текста."""
    base = lang_tag.split("-")[0].lower()
    if lang_tag in OCR_TO_NLLB:
        return OCR_TO_NLLB[lang_tag]
    if base in OCR_TO_NLLB:
        return OCR_TO_NLLB[base]
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
    han = sum(1 for c in text if "\u4E00" <= c <= "\u9FFF")
    kana = sum(1 for c in text if "\u3040" <= c <= "\u30FF")
    hang = sum(1 for c in text if "\uAC00" <= c <= "\uD7AF")
    if han >= 2:
        return "zho_Hans"
    if kana >= 2:
        return "jpn_Jpan"
    if hang >= 2:
        return "kor_Hang"
    if cyr >= 2:
        return "rus_Cyrl"
    return fallback


def capture_region(bbox) -> Image.Image:
    """Снять область экрана (left, top, width, height) в физических пикселях."""
    left, top, width, height = bbox
    if width < 2 or height < 2:
        raise ValueError("Слишком маленькая область выделения")
    return grab_display({"left": left, "top": top,
                         "width": width, "height": height})
