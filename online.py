"""Бесплатные онлайн-движки перевода (в паре с офлайн).

Работают без ключей: Google (неофициальный эндпоинт), MyMemory (лимит
~1000 слов/день), LibreTranslate (URL задаётся в config: libre_url).
HTTP-сессия переиспользуется (без рукопожатия TLS на каждый запрос),
ошибки логируются вместо полного молчания.
"""
import threading

import sosna_log

log = sosna_log.get()

# NLLB-код -> ISO-код для онлайн-движков
ISO = {
    "eng_Latn": "en", "rus_Cyrl": "ru", "ukr_Cyrl": "uk",
    "zho_Hans": "zh-CN", "jpn_Jpan": "ja", "kor_Hang": "ko",
    "deu_Latn": "de", "fra_Latn": "fr", "spa_Latn": "es",
    "ita_Latn": "it", "por_Latn": "pt", "pol_Latn": "pl",
    "tur_Latn": "tr",
}

LIBRE_URL = "https://libretranslate.de/translate"
TIMEOUT = 8            # чтение
CONNECT_TIMEOUT = 3    # подключение

_sess = None
_sess_lock = threading.Lock()


def _session():
    global _sess
    with _sess_lock:
        if _sess is None:
            import requests
            _sess = requests.Session()
        return _sess


def _iso(code: str) -> str:
    return ISO.get(code, code.split("_")[0].lower() if code else "en")


def google(text: str, src: str, tgt: str) -> str:
    r = _session().get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": _iso(src), "tl": _iso(tgt), "dt": "t", "q": text},
        timeout=(CONNECT_TIMEOUT, TIMEOUT),
    )
    r.raise_for_status()
    data = r.json()
    parts = data[0] if isinstance(data, list) and data else []
    return "".join(p[0] for p in parts if isinstance(p, list) and p and p[0])


def mymemory(text: str, src: str, tgt: str) -> str:
    r = _session().get(
        "https://api.mymemory.translated.net/get",
        params={"q": text[:500], "langpair": f"{_iso(src)}|{_iso(tgt)}"},
        timeout=(CONNECT_TIMEOUT, TIMEOUT),
    )
    r.raise_for_status()
    return r.json()["responseData"]["translatedText"]


def libretranslate(text: str, src: str, tgt: str, url: str | None = None) -> str:
    import config
    if not url:
        url = (config.load().get("libre_url") or "").strip() or LIBRE_URL
    r = _session().post(
        url,
        json={"q": text, "source": _iso(src), "target": _iso(tgt), "format": "text"},
        timeout=(CONNECT_TIMEOUT, TIMEOUT),
    )
    r.raise_for_status()
    return r.json()["translatedText"]


def translate(text: str, src: str, tgt: str, engine: str) -> str:
    """Перевести одним онлайн-движком. Возвращает '' при любой ошибке."""
    try:
        if engine == "google":
            return google(text, src, tgt)
        if engine == "mymemory":
            return mymemory(text, src, tgt)
        if engine == "libretranslate":
            return libretranslate(text, src, tgt)
    except Exception as e:
        log.warning("онлайн-движок %s не ответил: %s", engine, e)
        return ""
    return ""


def translate_many(texts: list, src: str, tgt: str, engine: str) -> list:
    return [translate(t, src, tgt, engine) for t in texts]
