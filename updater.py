"""Обновление баз переводчика «Сосны» (модели движков с HuggingFace).

Надёжность: частично скачанные файлы НЕ удаляются (докачка со следующей
попытки вместо старта с нуля), перед скачиванием проверяется свободное
место, ошибки сети честно отличаются от «репозиторий недоступен»,
модель перезагружается только если базы реально изменились. Манифесты
пишутся атомарно. Автообновление НЕ качает большие отсутствующие базы
без согласия — только уведомляет.
"""
import json
import os
import shutil
import threading
import time

import config
import sosna_log

log = sosna_log.get()

UPDATE_CHECK_INTERVAL = 6 * 3600  # подстраховка, если интервал в конфиге неверный


def _engine_spec(name=None):
    """Репозиторий и папка активного (или указанного) движка."""
    import translate
    name = name or translate.active_engine()
    spec = config.ENGINES[name]
    return spec["repo"], os.path.join(config.MODELS_DIR, spec["dir"])


def _manifest_path(engine_dir):
    return os.path.join(engine_dir, "installed.json")


def _read_manifest(engine_dir):
    # миграция: старый общий манифест переезжает в папку движка NLLB-600M
    legacy = os.path.join(config.MODELS_DIR, "installed.json")
    target = _manifest_path(engine_dir)
    if not os.path.exists(target) and os.path.exists(legacy) and "nllb-ct2" in engine_dir:
        try:
            shutil.move(legacy, target)
        except OSError:
            pass
    data, _src = config.load_json_with_bak(target)
    return data if isinstance(data, dict) else None


def _write_manifest(engine_dir, data):
    os.makedirs(engine_dir, exist_ok=True)
    config.save_json_atomic(_manifest_path(engine_dir), data)


def is_online(timeout=5):
    try:
        import requests
        requests.head("https://huggingface.co", timeout=timeout)
        return True
    except Exception:
        return False


def _net_error(e: Exception) -> str:
    """Классифицировать ошибку сети: нет интернета или сервер недоступен."""
    import requests
    if isinstance(e, (requests.exceptions.ConnectionError,
                      requests.exceptions.Timeout,
                      requests.exceptions.SSLError)):
        return "Нет подключения к интернету"
    return f"Сайт HuggingFace недоступен ({type(e).__name__})"


def check_for_updates(all_engines=True):
    """Есть ли обновления. all_engines=False — только активный движок (автопроверка)."""
    import translate
    if not is_online():
        return None, "Нет подключения к интернету"
    names = list(config.ENGINES) if all_engines else [translate.active_engine()]
    missing, stale, actual = [], [], []
    for name in names:
        need, msg = _check_one(name)
        if need is None:
            return None, msg
        if not need:
            actual.append(name)
        elif "не установлена" in msg:
            missing.append(name)
        else:
            stale.append(name)
    if missing or stale:
        parts = [f"скачать базу {config.ENGINES[n]['short']}" for n in missing]
        parts += [f"обновить {config.ENGINES[n]['short']}" for n in stale]
        return True, "Требуется: " + ", ".join(parts)
    return False, "Все базы актуальны"


def _check_one(name):
    """(need_update, описание) для одного движка. need_update=None — сеть/сервер."""
    from huggingface_hub import HfApi
    repo, engine_dir = _engine_spec(name)
    spec = config.ENGINES[name]
    try:
        info = HfApi().model_info(repo, files_metadata=True)
    except Exception as e:
        log.warning("проверка %s не удалась: %s", repo, e)
        return None, _net_error(e)
    remote = {s.rfilename: s.blob_id for s in info.siblings if s.rfilename != ".gitattributes"}
    if not os.path.exists(os.path.join(engine_dir, "model.bin")):
        return True, f"База {spec['short']} не установлена ({len(remote)} файлов)"
    installed = _read_manifest(engine_dir)
    if not installed:
        # файлы есть (скачаны вручную), манифеста нет — просто запишем его
        return False, f"База {spec['short']} установлена"
    if installed.get("revision") != info.sha:
        return True, f"Для {spec['short']} доступна новая версия ({info.sha[:8]})"
    changed = [f for f, bid in remote.items() if installed.get("files", {}).get(f) != bid]
    if changed:
        return True, f"В {spec['short']} обновились файлы: {', '.join(changed[:5])}"
    return False, f"База {spec['short']} актуальна"


def _free_mb_ok(engine_dir, need_mb: int) -> bool:
    """Хватает ли места (с запасом x2 под замену каталога)."""
    try:
        usage = shutil.disk_usage(os.path.dirname(engine_dir) or ".")
        return usage.free >= need_mb * 2 * 1024 * 1024
    except OSError:
        return True  # не смогли проверить — не блокируем


def update(progress=None, all_engines=True):
    """Скачать/обновить базы движков. Возвращает (сообщение, changed).

    all_engines=False — только активный (авто). Кнопка «Обновить базы»
    качает ВСЕ недостающие движки, а не только активный.
    """
    import translate

    def report(msg):
        log.info("обновление: %s", msg)
        if progress:
            progress(msg)

    if not is_online():
        raise RuntimeError("Нет подключения к интернету")

    names = list(config.ENGINES) if all_engines else [translate.active_engine()]
    downloaded, recorded, actual, failed = [], [], [], []
    for name in names:
        spec = config.ENGINES[name]
        repo, engine_dir = _engine_spec(name)
        installed = _read_manifest(engine_dir)
        have_model = os.path.exists(os.path.join(engine_dir, "model.bin"))
        stale = False
        if installed and have_model:
            need, msg = _check_one(name)
            if need is None:
                raise RuntimeError(msg)
            stale = need and "не установлена" not in msg

        try:
            if not have_model or stale:
                size = spec.get("size_mb", 600)
                if not _free_mb_ok(engine_dir, size):
                    failed.append(f"{spec['short']} (нет места на диске)")
                    continue
                action = "Скачиваю" if not have_model else "Обновляю"
                report(f"{action} базу {spec['short']} (~{size} МБ), это несколько минут...")
                _sync_engine(name)
                downloaded.append(spec["short"])
            elif installed is None and have_model:
                report(f"Записываю манифест для {spec['short']}...")
                _write_manifest_only(name)
                recorded.append(spec["short"])
            else:
                actual.append(spec["short"])
        except Exception as e:
            log.exception("обновление %s не удалось", name)
            failed.append(f"{spec['short']} ({e})")

    parts = []
    if downloaded:
        parts.append("Скачано: " + ", ".join(downloaded))
    if recorded:
        parts.append("Зарегистрировано: " + ", ".join(recorded))
    if actual:
        parts.append("Актуальны: " + ", ".join(actual))
    if failed:
        parts.append("НЕ обновлены: " + ", ".join(failed))
    result = "; ".join(parts) if parts else "Базы не изменились"
    report("Готово. " + result)

    # модель перезагружаем только если активная база реально изменилась
    changed = bool(downloaded)
    return result, changed


def _sync_engine(name):
    """Скачать репозиторий движка во временную папку и атомарно заменить.

    Частичная загрузка в .tmp НЕ удаляется при сбое — в следующий раз
    докачается, а не начнётся с нуля.
    """
    from huggingface_hub import snapshot_download
    repo, engine_dir = _engine_spec(name)
    tmp_dir = engine_dir + ".tmp"
    try:
        # .tmp оставляем как есть: huggingface_hub докачает недостающее
        snapshot_download(repo, local_dir=tmp_dir)
    except Exception as e:
        raise RuntimeError(f"Ошибка скачивания {name}: {e}") from e

    bak_dir = engine_dir + ".bak"
    if os.path.exists(bak_dir):
        shutil.rmtree(bak_dir, ignore_errors=True)
    if os.path.exists(engine_dir):
        try:
            os.rename(engine_dir, bak_dir)
        except OSError as e:
            # файл модели открыт загруженным CTranslate2 / антивирусом —
            # скачивание не пропадёт (.tmp сохранён), попробуем позже
            raise RuntimeError(
                f"Не удалось заменить базу {name} (файл занят): {e}") from e
    try:
        os.rename(tmp_dir, engine_dir)
    except OSError:
        if os.path.exists(bak_dir) and not os.path.exists(engine_dir):
            os.rename(bak_dir, engine_dir)
        raise
    shutil.rmtree(bak_dir, ignore_errors=True)
    _write_manifest_only(name)

    # починка токенизатора Opus-MT после свежей загрузки
    import translate
    if translate._is_marian(name):
        translate.ensure_tokenizer_files(name)


def _write_manifest_only(name):
    """Записать манифест для уже скачанных файлов (без повторной загрузки)."""
    from huggingface_hub import HfApi
    repo, engine_dir = _engine_spec(name)
    os.makedirs(engine_dir, exist_ok=True)
    try:
        info = HfApi().model_info(repo, files_metadata=True)
        _write_manifest(engine_dir, {
            "repo": repo,
            "revision": info.sha,
            "updated_at": time.strftime("%Y-%m-%d %H:%M"),
            "files": {s.rfilename: s.blob_id for s in info.siblings
                      if s.rfilename != ".gitattributes"},
        })
    except Exception as e:
        # манифест некритичен (база на диске), но молчать не будем
        log.warning("манифест %s не записан: %s", name, e)


class AutoUpdater(threading.Thread):
    """Фоновый цикл автообновления (только активный движок).

    Большая отсутствующая база НЕ скачивается без согласия — только
    уведомление. Обновление уже установленной базы (тот же размер) — качается.
    """

    def __init__(self, status_cb=None):
        super().__init__(name="sosna-updater", daemon=True)
        self.status_cb = status_cb
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _sleep(self, seconds):
        self._stop.wait(seconds)

    def _report(self, msg):
        log.info("автообновление: %s", msg)
        if self.status_cb:
            try:
                self.status_cb(msg)
            except Exception:
                log.exception("status_cb автообновления упал")

    def run(self):
        cfg = config.load()
        # стартовая проверка через 30с после запуска, потом по расписанию
        self._sleep(30)
        while not self._stop.is_set():
            if cfg.get("auto_update", True):
                try:
                    need, msg = check_for_updates(all_engines=False)
                    if need:
                        if "не установлена" in msg:
                            self._report(
                                "База активного движка не скачана — "
                                "трей → Настройки → «Обновить базы»")
                        else:
                            result, changed = update(self.status_cb, all_engines=False)
                            if changed:
                                import translate
                                translate.reload()
                    elif msg != "Все базы актуальны":
                        self._report(msg)
                except Exception:
                    log.exception("цикл автообновления")
            # перечитываем конфиг — интервал могли поменять
            cfg = config.load()
            interval = max(1, int(cfg.get("update_interval_hours", 24))) * 3600
            self._sleep(interval if cfg.get("auto_update", True) else UPDATE_CHECK_INTERVAL)
