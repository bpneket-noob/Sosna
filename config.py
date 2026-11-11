"""Конфигурация «Сосны»: загрузка/сохранение config.json.

Пользовательские данные (конфиг, история, глоссарий, логи) живут в
%APPDATA%\\Sosna и переживают удаление/переустановку программы.
Запись атомарная (tmp + os.replace) с автоматическим .bak — сбой
в момент записи больше не стирает настройки. Загрузка валидирует
типы и значения: кривой ручной конфиг не роняет приложение.
"""
import json
import os
import shutil
import threading

APP_VERSION = "1.3.10"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Пользовательские данные — в %APPDATA% (переживают переустановку)
DATA_DIR = os.path.join(os.environ.get("APPDATA") or BASE_DIR, "Sosna")
LOG_DIR = os.path.join(DATA_DIR, "logs")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
MODELS_DIR = os.path.join(BASE_DIR, "models")

# Доступные движки перевода (переключаются в настройках)
ENGINES = {
    "opus-mt": {
        "dir": "opus-mt-en-ru",
        "repo": "jiangzhuo9357/opus-mt-en-ru-ct2",
        "label": "⚡ Opus-MT — быстро, среднее качество (~0.3 ГБ, свободная лицензия)",
        "short": "Opus-MT",
        "size_mb": 300,
    },
    "nllb-600M": {
        "dir": "nllb-ct2",
        "repo": "JustFrederik/nllb-200-distilled-600M-ct2-int8",
        "label": "🌲 NLLB — средняя скорость, хорошее качество (~1 ГБ)",
        "short": "NLLB",
        "size_mb": 600,
    },
    "nllb-1.3B": {
        "dir": "nllb-1.3b-ct2",
        "repo": "JustFrederik/nllb-200-distilled-1.3B-ct2-int8",
        "label": "🏆 NLLB-1.3B — медленно, отличное качество (~2 ГБ)",
        "short": "NLLB-1.3B",
        "size_mb": 1300,
    },
}

DEFAULTS = {
    "hotkey": "ctrl+alt+t",             # перевод фрагмента
    "hotkey_fullscreen": "ctrl+alt+f",  # перевод всего экрана
    "hotkey_live": "ctrl+alt+l",        # живые субтитры
    "hotkey_repeat": "ctrl+alt+r",      # повторить последний перевод
    "engine": "nllb-600M",              # активный движок перевода
    "ocr_lang": "auto",         # язык распознавания Windows OCR ("auto" — определять)
    "nllb_src": "eng_Latn",     # исходный язык NLLB (используется, если OCR не «auto»)
    "nllb_tgt": "rus_Cyrl",     # целевой язык NLLB
    "online_engine": "off",     # off | google | mymemory | libretranslate
    "online_mode": "fallback",  # fallback | boost | online
    "live_mode": "fast",        # fast | quality — режим живых субтитров
    "live_interval": 2,         # сек между циклами живых субтитров
    "live_lines": 1,            # сколько последних фраз показывать в окне (1–3)
    "live_wait_sentence": True, # переводить фразу только после знака конца
    "profile": "balance",       # weak | balance | power
    "ui_theme": "komi",         # komi | light | dark — тема настроек
    "auto_update": True,
    "update_interval_hours": 24,
    "result_timeout_sec": 60,   # автозакрытие окна результата, 0 = не закрывать
    "model_in_ram": True,       # держать базу в ОЗУ постоянно
    "model_idle_min": 5,        # ...если выключено: минут в памяти после перевода
    "libre_url": "",            # свой сервер LibreTranslate (пусто — публичный)
}

# Профили: пресеты настроек под разные компьютеры
PROFILES = {
    "weak": {
        "label": "💻 Слабый ПК",
        "values": {"engine": "opus-mt", "model_in_ram": False,
                   "live_mode": "fast", "live_interval": 2},
    },
    "balance": {
        "label": "⚖️ Баланс",
        "values": {"engine": "nllb-600M", "model_in_ram": True,
                   "live_mode": "fast", "live_interval": 2},
    },
    "power": {
        "label": "🚀 Мощный ПК",
        "values": {"engine": "nllb-1.3B", "model_in_ram": True,
                   "live_mode": "quality", "live_interval": 2},
    },
}

_lock = threading.Lock()
_cache = None

# границы числовых настроек: (мин, макс)
_INT_RANGES = {
    "live_interval": (1, 30),
    "live_lines": (1, 3),
    "update_interval_hours": (1, 168),
    "result_timeout_sec": (0, 86400),
    "model_idle_min": (0, 1440),
}
_BOOL_KEYS = ("auto_update", "model_in_ram", "live_wait_sentence")
_STR_KEYS = ("hotkey", "hotkey_fullscreen", "hotkey_live", "hotkey_repeat",
             "ocr_lang", "nllb_src", "nllb_tgt", "online_engine",
             "online_mode", "live_mode", "profile", "ui_theme", "libre_url")


def _migrate_legacy_files():
    """Одноразовая миграция config/history/glossary из папки установки."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        for name in ("config.json", "history.json", "glossary.json"):
            src = os.path.join(BASE_DIR, name)
            dst = os.path.join(DATA_DIR, name)
            if not os.path.exists(dst) and os.path.exists(src):
                shutil.copy2(src, dst)
    except OSError:
        pass  # миграция не критична: дальше сработают дефолты/пустые файлы


def save_json_atomic(path: str, data, indent: int = 2):
    """Атомарная запись JSON: tmp + fsync + os.replace, прежний файл → .bak.

    Читатель в любой момент видит либо старый, либо новый цельный файл.
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    os.replace(tmp, path)


def load_json_with_bak(path):
    """Прочитать JSON; если файл повреждён — попробовать его .bak.

    Возвращает (data, источник) или (None, None). Читаем utf-8-sig:
    файл, сохранённый Блокнотом/PowerShell с BOM, иначе тихо
    превращался в «повреждённый» и настройки сбрасывались.
    """
    for p in (path, path + ".bak"):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    return json.load(f), p
            except (json.JSONDecodeError, OSError, ValueError):
                continue
    return None, None


def _validate(cfg: dict) -> dict:
    """Привести значения к нужным типам и границам; мусор → дефолт."""
    optional = ("hotkey_fullscreen", "hotkey_live", "hotkey_repeat", "libre_url")
    for key in _STR_KEYS:
        v = cfg.get(key)
        if isinstance(v, str):
            cfg[key] = v.strip()
        else:
            # мусорный тип: обязательные — дефолт, опциональные — «выключено»
            cfg[key] = "" if key in optional else str(DEFAULTS[key])
        if key in optional:
            continue  # эти могут быть пустыми (выключено)
        if not cfg[key]:
            cfg[key] = DEFAULTS[key]
    for key, (lo, hi) in _INT_RANGES.items():
        v = cfg.get(key)
        try:
            cfg[key] = max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            cfg[key] = DEFAULTS[key]
    for key in _BOOL_KEYS:
        cfg[key] = bool(cfg.get(key, DEFAULTS[key]))
    if cfg.get("engine") not in ENGINES:
        cfg["engine"] = DEFAULTS["engine"]
    if cfg.get("profile") not in PROFILES:
        cfg["profile"] = DEFAULTS["profile"]
    if cfg.get("ui_theme") not in ("komi", "light", "dark"):
        cfg["ui_theme"] = DEFAULTS["ui_theme"]
    if cfg.get("online_engine") not in ("off", "google", "mymemory", "libretranslate"):
        cfg["online_engine"] = DEFAULTS["online_engine"]
    if cfg.get("online_mode") not in ("fallback", "boost", "online"):
        cfg["online_mode"] = DEFAULTS["online_mode"]
    if cfg.get("live_mode") not in ("fast", "quality"):
        cfg["live_mode"] = DEFAULTS["live_mode"]
    return cfg


def load():
    """Конфиг (кеш в памяти; пишем только через save — кеш согласован)."""
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        _migrate_legacy_files()  # разовая миграция из папки установки
        cfg = dict(DEFAULTS)
        user, _src = load_json_with_bak(CONFIG_PATH)
        if user is not None and isinstance(user, dict):
            for k in DEFAULTS:
                if k in user:
                    cfg[k] = user[k]
        cfg = _validate(cfg)
        _cache = cfg
        return dict(cfg)


def save(cfg):
    global _cache
    with _lock:
        # склейка с дефолтами: частичный dict не должен стирать пропущенные ключи
        clean = _validate({**DEFAULTS, **dict(cfg)})
        _cache = dict(clean)
        save_json_atomic(CONFIG_PATH, clean)
    return clean


def reset_cache():
    """Забыть кеш и перечитать с диска (диагностика/тесты)."""
    global _cache
    with _lock:
        _cache = None
