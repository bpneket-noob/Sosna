"""Офлайн-перевод: переключаемые движки (NLLB-200 / Opus-MT) через CTranslate2.

Режимы памяти (config):
  model_in_ram=True  — база грузится на старте и живёт постоянно;
  model_in_ram=False — «умный таймер»: база грузится при первом переводе
      и остаётся в памяти model_idle_min минут после последнего
      (сессия живых субтитров удерживает базу всё время работы).

Потокобезопасность: весь жизненный цикл и переводы идут под одним RLock —
параллельные загрузки двух копий модели и выгрузка во время перевода
(раньше AttributeError) невозможны. Токенизатор мутируется только под
локом, гонки src_lang больше нет.
"""
import gc
import os
import re
import threading
import time

import config
import sosna_log

log = sosna_log.get()

# пробел перед пунктуацией — типовой артефакт детокенизации NLLB
_PUNCT_SPACE = re.compile(r"\s+([,.!?;:%)])")
# подтёкшие служебные токены языков («rus_Cyrl», «eng_Latn»)
_LANG_TAG = re.compile(r"\b[a-z]{3}_[A-Z][a-z]{3,}\b")


def postprocess(text: str) -> str:
    """Починить типовые артефакты машинного перевода.

    «на цель .» → «на цель.»; двойные пробелы; «25 %» → «25%»;
    случайные токены языков в начале вывода.
    """
    if not text:
        return text
    text = _LANG_TAG.sub("", text)
    text = _PUNCT_SPACE.sub(r"\1", text)
    text = text.replace("( ", "(")
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()

_translator = None
_tokenizer = None
_loaded_engine = None
_error = None                  # причина последнего сбоя загрузки (для UI)
_state = threading.Event()     # модель загружена и готова
_lock = threading.RLock()      # сериализует lifecycle + переводы
_hold = 0                      # внешние удержания (live-сессия и т.п.)
_last_used = 0.0               # момент последнего перевода
_idle_thread_started = False
_IDLE_CHECK = 10.0             # период проверки таймера выгрузки, сек


def active_engine() -> str:
    name = config.load().get("engine", "nllb-600M")
    return name if name in config.ENGINES else "nllb-600M"


def engine_dir(name: str | None = None) -> str:
    spec = config.ENGINES[name or active_engine()]
    return os.path.join(config.MODELS_DIR, spec["dir"])


def engine_present(name: str | None = None) -> bool:
    return os.path.exists(os.path.join(engine_dir(name), "model.bin"))


def model_present() -> bool:
    return engine_present()


def is_ready() -> bool:
    return _state.is_set()


def get_error() -> str | None:
    with _lock:
        return _error


def _is_marian(name: str) -> bool:
    return (name or "").startswith("opus")


def ensure_tokenizer_files(engine: str | None = None):
    """Починка файлов токенизатора для CT2-репозиториев Opus-MT.

    В репозиториях ct2 словарь лежит списком (shared_vocabulary.json), а
    MarianTokenizer ждёт словарь vocab.json + tokenizer_config.json.
    """
    d = engine_dir(engine)
    if not os.path.isdir(d):
        return
    import json
    vocab = os.path.join(d, "vocab.json")
    shared = os.path.join(d, "shared_vocabulary.json")
    cfg = os.path.join(d, "tokenizer_config.json")
    if not os.path.exists(vocab) and os.path.exists(shared):
        with open(shared, "r", encoding="utf-8") as f:
            words = json.load(f)
        config.save_json_atomic(vocab, {w: i for i, w in enumerate(words)})
    if not os.path.exists(cfg):
        config.save_json_atomic(cfg, {"tokenizer_class": "MarianTokenizer",
                                      "model_max_length": 512})


# ---------- lifecycle (всё под _lock) ----------

def _do_load_locked():
    """Загрузить движок с диска. Бросает исключение при сбое."""
    global _translator, _tokenizer, _error, _loaded_engine
    name = active_engine()
    d = engine_dir(name)
    if not os.path.exists(os.path.join(d, "model.bin")):
        raise RuntimeError(
            f"База {config.ENGINES[name]['short']} не скачана — "
            "трей → Настройки → «Обновить базы»")
    t0 = time.time()
    import logging as _logging
    _logging.getLogger("transformers").setLevel(_logging.ERROR)
    import ctranslate2
    import transformers

    log.info("загружаю движок %s...", name)
    translator = ctranslate2.Translator(d, device="cpu", inter_threads=2)
    marian = _is_marian(name)
    if marian:
        ensure_tokenizer_files(name)
        from transformers import MarianTokenizer
        tokenizer = MarianTokenizer.from_pretrained(d)
    else:
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            d, src_lang=config.DEFAULTS["nllb_src"])

    # прогрев: первый translate_batch инициализирует ленивые структуры —
    # делаем это здесь, а не на первом пользовательском переводе
    try:
        if marian:
            batch = [tokenizer.convert_ids_to_tokens(tokenizer.encode("warmup"))]
            translator.translate_batch(batch, max_batch_size=8, beam_size=2)
        else:
            tokenizer.src_lang = config.DEFAULTS["nllb_src"]
            batch = [tokenizer.convert_ids_to_tokens(tokenizer.encode("warmup"))]
            translator.translate_batch(batch, max_batch_size=8, beam_size=2,
                                       target_prefix=[[config.DEFAULTS["nllb_tgt"]]])
    except Exception as e:  # прогрев не критичен, но логируем
        log.warning("прогрев движка не удался (не критично): %s", e)

    _translator, _tokenizer = translator, tokenizer
    _loaded_engine = name
    _error = None
    _state.set()
    log.info("движок %s готов за %.1fs%s",
             name, time.time() - t0, " (с прогревом)" if not marian else "")


def _ensure_loaded_locked():
    """Загрузить движок, если ещё не готов. Ошибка прошлой попытки не «липнет»."""
    global _error
    if _state.is_set():
        return
    _error = None  # даём повторную попытку после сбоя
    _do_load_locked()


def _do_unload_locked():
    global _translator, _tokenizer, _loaded_engine, _error
    if _translator is None:
        return
    name = _loaded_engine
    _translator = None
    _tokenizer = None
    _loaded_engine = None
    _state.clear()
    gc.collect()
    log.info("движок %s выгружен из памяти", name)


def _maybe_start_idle_thread_locked():
    global _idle_thread_started
    if _idle_thread_started:
        return
    _idle_thread_started = True

    def idle_watcher():
        while True:
            time.sleep(_IDLE_CHECK)
            try:
                with _lock:
                    if not _state.is_set() or _hold > 0:
                        continue
                    cfg = config.load()
                    if cfg.get("model_in_ram", True):
                        continue
                    idle_min = int(cfg.get("model_idle_min", 5))
                    if idle_min <= 0:
                        continue  # 0 обрабатывается сразу после перевода
                    deadline = _last_used + idle_min * 60
                    if time.time() >= deadline:
                        _do_unload_locked()
            except Exception:
                log.exception("сбой потока таймера выгрузки")

    threading.Thread(target=idle_watcher, name="sosna-model-idle",
                     daemon=True).start()


def preload():
    """Загрузить активный движок в фоне (не блокирует вызывателя)."""
    def run():
        try:
            with _lock:
                _maybe_start_idle_thread_locked()
                _ensure_loaded_locked()
        except Exception as e:
            log.error("предзагрузка движка не удалась: %s", e)
    t = threading.Thread(target=run, name="sosna-model-load", daemon=True)
    t.start()
    return t


def wait_ready(timeout=None) -> bool:
    return _state.wait(timeout)


def hold():
    """Удерживать модель в памяти (live-сессия). Парный вызов — release()."""
    global _hold
    with _lock:
        _hold += 1
        log.debug("удержание модели: %d", _hold)


def release():
    global _hold
    with _lock:
        _hold = max(0, _hold - 1)
        log.debug("удержание модели снято: %d", _hold)


def unload():
    """Выгрузить модель (ждёт завершения идущего перевода — безопасно)."""
    with _lock:
        _do_unload_locked()


def reload():
    """Перезагрузить модель (после обновления базы / смены движка)."""
    with _lock:
        _do_unload_locked()
    if config.load().get("model_in_ram", True):
        preload()


def get_loaded_engine():
    with _lock:
        return _loaded_engine


# ---------- перевод ----------

def _norm_key(s: str) -> str:
    """Ключ сравнения без пунктуации и регистра (для детекта «эха»)."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _decode_hyp(out_tokens, tgt_lang, marian: bool) -> str:
    decoded = _tokenizer.decode(
        _tokenizer.convert_tokens_to_ids(out_tokens), skip_special_tokens=True
    )
    if not marian:
        # NLLB начинает ответ с токена целевого языка — убираем его
        if decoded.startswith(tgt_lang + " "):
            decoded = decoded[len(tgt_lang) + 1:]
    return decoded.strip()


def _decode_best(result_item, tgt_lang, marian: bool, src_text: str) -> str:
    """Выбрать гипотезу, не повторяющую источник.

    NLLB иногда «эхом» возвращает оригинал вместо перевода — берём
    первую гипотезу, которая переводом является; эхо — запасной вариант.
    """
    fallback = None
    src_key = _norm_key(src_text)
    for hyp in result_item.hypotheses[:3]:
        text = _decode_hyp(hyp, tgt_lang, marian)
        if fallback is None:
            fallback = text
        if text and _norm_key(text) != src_key:
            return text
    return fallback or ""


def translate(text: str, src_lang: str, tgt_lang: str) -> str:
    """Перевести один текст активным движком."""
    return translate_many([text], src_lang, tgt_lang)[0]


def translate_many(texts: list, src_lang: str, tgt_lang: str) -> list:
    """Перевести список текстов одним батчем активным движком."""
    global _last_used
    with _lock:
        _ensure_loaded_locked()
        _maybe_start_idle_thread_locked()
        marian = _is_marian(_loaded_engine)
        t0 = time.time()
        try:
            if not marian:
                _tokenizer.src_lang = src_lang
            batch = [_tokenizer.convert_ids_to_tokens(_tokenizer.encode(t))
                     for t in texts]
            # beam 4 для быстрых движков даёт стабильнее результат; для
            # тяжёлого 1.3B хватит beam 2 (его качество и так выше,
            # а decode вдвое быстрее — заметно в живых субтитрах)
            beam = 2 if "1.3B" in (_loaded_engine or "") else 4
            kwargs = {"max_batch_size": 8, "beam_size": beam,
                      "repetition_penalty": 1.05}
            if not marian:
                kwargs["target_prefix"] = [[tgt_lang]] * len(batch)
            results = _translator.translate_batch(batch, **kwargs)
            outs = [_decode_best(r, tgt_lang, marian, src_text)
                    for src_text, r in zip(texts, results)]
        finally:
            _last_used = time.time()
        dt = _last_used - t0
        log.debug("переведено %d текстов за %.2fs (движок %s)",
                  len(texts), dt, _loaded_engine)
        # мгновенная выгрузка — только если пользователь выбрал 0 минут
        cfg = config.load()
        if (not cfg.get("model_in_ram", True)
                and int(cfg.get("model_idle_min", 5)) <= 0 and _hold == 0):
            _do_unload_locked()
        return outs


# ---------- умный конвейер: офлайн + онлайн в паре + глоссарий ----------

def smart_translate(texts: list, src_lang: str, tgt_lang: str) -> list:
    """Перевод с учётом настроек онлайн-движка и глоссария.

    online_engine: off | google | mymemory | libretranslate
    online_mode:   fallback (офлайн-первый, онлайн при сбое) |
                   boost (офлайн, затем онлайн улучшает результат) |
                   online (только онлайн)
    """
    import glossary
    cfg = config.load()
    online_engine = cfg.get("online_engine", "off")
    online_mode = cfg.get("online_mode", "fallback")
    entries = glossary.load()

    if src_lang == tgt_lang:
        # исходный язык совпадает с целевым: перевод не нужен,
        # оставляем текст как есть (применяя только глоссарий)
        return [glossary.apply(t, entries) for t in texts]

    if online_engine != "off" and online_mode == "online":
        import online
        outs = online.translate_many(texts, src_lang, tgt_lang, online_engine)
        return [glossary.apply(postprocess(t or s), entries)
                for t, s in zip(outs, texts)]

    try:
        outs = translate_many(texts, src_lang, tgt_lang)
    except Exception as e:
        log.error("офлайн-перевод не удался: %s", e)
        if online_engine == "off":
            raise
        outs = [""] * len(texts)

    if online_engine != "off":
        import online
        for i, t in enumerate(outs):
            # fallback: онлайн дёргаем ТОЛЬКО когда офлайн не справился —
            # раньше запрос уходил на каждый текст и результат выбрасывался
            need_online = (online_mode == "boost") or \
                          (online_mode == "fallback" and not t.strip())
            if not need_online:
                continue
            fixed = online.translate(texts[i], src_lang, tgt_lang, online_engine)
            if fixed:
                outs[i] = fixed

    return [glossary.apply(postprocess(t), entries) for t in outs]


def smart_translate_one(text: str, src_lang: str, tgt_lang: str) -> str:
    return smart_translate([text], src_lang, tgt_lang)[0]
