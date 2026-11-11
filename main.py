"""«Сосна» — офлайн-переводчик выделенного фрагмента экрана.

Запуск: ярлык (runtime\\Sosna.exe main.py) или start_sosna.bat
Трей: перевод, повтор, субтитры, история, настройки, обновление баз, выход.

Ядро стабильности: единая очередь для вызовов в UI-поток (root.after из
фоновых потоков небезопасен), защита от второго экземпляра, перехват всех
ошибок в лог, аккуратный выход с освобождением хоткеев/модели.
"""
import ctypes
import queue
import sys
import threading
import time

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # физические пиксели
except Exception:
    pass

import tkinter as tk

import config
import hotkeys_win
import sosna_log

# логи — до всего остального: даже падение на импортах останется в файле
log = sosna_log.setup(config.DATA_DIR)
sosna_log.install_excepthooks()
log.info("=== Сосна v%s запускается ===", config.APP_VERSION)

import glossary
import history
import live
import ocr
import screen_view
import selector
import settings_window
import translate
import result_window
import updater

DONATE_URL = "https://www.donationalerts.com/r/unclenikita"

_MUTEX_NAME = "Local\\SosnaAppSingleInstance"
_ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance():
    """Windows mutex: True — мы первые и единственные, False — уже запущены."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return True  # не смогли создать — лучше запуститься, чем не работать
        return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS
    except Exception:
        return True


def make_icon_image():
    """Иконка-«сосна» из assets (при отсутствии — простая рисованая)."""
    import os
    from PIL import Image, ImageDraw
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "sosna_icon_64.png")
    if os.path.exists(path):
        return Image.open(path).convert("RGBA")
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    green = (46, 125, 50, 255)
    dark = (27, 94, 32, 255)
    for i, w in enumerate((48, 38, 28)):
        y = 6 + i * 14
        d.polygon([(32 - w // 2, y + 16), (32 + w // 2, y + 16), (32, y)], fill=green)
        d.line([(32, y), (32, y + 16)], fill=dark)
    d.rectangle([28, 50, 36, 62], fill=(121, 85, 72, 255))
    return img


class SosnaApp:
    def __init__(self):
        self.cfg = config.load()
        self.root = tk.Tk()
        self.root.withdraw()  # корень никогда не показываем
        sosna_log.install_tk_hook(self.root)  # ошибки в колбэках кнопок — в лог
        self.tray = None
        self._auto_updater = None
        self._hk_lock = threading.Lock()
        self._result_win = None
        self._translating = False
        self._live = None          # активная сессия живых субтитров
        self._live_booting = False # защита от двойного запуска live
        self._last_capture = None  # (bbox, lang_tag, src) для «повторить»
        # Все вызовы из фоновых потоков в UI идут через одну очередь,
        # опрашиваемую таймером в главном потоке (root.after из чужого
        # потока небезопасен и убивал хоткеи после нескольких срабатываний)
        self._ui_queue = queue.Queue()
        # хоткеи: колбэк библиотеки keyboard кладёт функцию в очередь
        self._hk_queue = queue.Queue()
        self._hotkeys_suspended = False
        self._hk_last_seen = time.time()  # пульс клавиатуры для сторожа
        self._hk_seq = 0                  # счётчик дошедших хоткеев
        self._hk_map = []                 # LL-канал: [(коды_частей, строка, действие)]
        self._pressed = set()             # зажатые scan-коды (из событий down/up)
        self._fired = set()               # комбинации, сработавшие в этом зажатии
        # RegisterHotKey-канал (как у NVIDIA): системная доставка WM_HOTKEY,
        # не зависит от загрузки процесса; LL-детектор — резерв
        self._hk_disp = hotkeys_win.HotkeyDispatcher(
            on_hotkey=self._on_reg_hotkey,
            on_failed=lambda c: self.ui_schedule(
                lambda: result_window.show_message(
                    self.root, f"Сосна: комбинация {c} занята другим "
                               f"приложением — работает резервный режим", 6)))
        self._hk_reg_fn = {}              # combo_str → действие (RegHotKey-канал)
        self._hk_fired_at = {}            # combo_str → ts последнего срабатывания

    # ---------- маршалинг в UI-поток ----------

    def ui_schedule(self, fn):
        """Потокобезопасно выполнить fn в UI-потоке."""
        self._ui_queue.put(fn)

    def _poll_ui(self):
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("сбой задачи UI-очереди")
        except queue.Empty:
            pass
        self.root.after(50, self._poll_ui)

    # ---------- хоткеи ----------

    def _on_hotkey(self, fn, combo: str | None = None):
        """Поставить действие хоткея в очередь (единая точка обоих каналов).

        Дедупликация каналов: RegisterHotKey и LL-детектор видят одно
        нажатие — фиксируем момент срабатывания по комбинации и пускаем
        только первое (второе в окне 0.5с — дубль).
        """
        if self._hotkeys_suspended:
            return
        try:
            if combo is not None:
                now = time.time()
                if now - self._hk_fired_at.get(combo, 0) < 0.5:
                    log.debug("хоткей %s: сработал другой канал — дубликат",
                              combo)
                    return
                self._hk_fired_at[combo] = now
            self._hk_seq += 1
            self._hk_queue.put(fn)
        except Exception:
            log.exception("не удалось поставить хоткей в очередь")

    def suspend_hotkeys(self):
        """Отключить хоткеи (на время записи новой комбинации): оба канала."""
        with self._hk_lock:
            self._hotkeys_suspended = True
        self._hk_disp.update([])   # RegisterHotKey-канал тоже молчит

    def resume_hotkeys(self):
        """Вернуть хоткеи по текущему конфигу (оба канала)."""
        with self._hk_lock:
            self._hotkeys_suspended = False
        self.register_hotkey()
        log.info("хоткеи возвращены")

    def _poll_hotkeys(self):
        try:
            while True:
                fn = self._hk_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("сбой обработчика хоткея")
        except queue.Empty:
            pass
        self.root.after(80, self._poll_hotkeys)

    # ---------- сторож хоткеев ----------

    def start_hotkey_watchdog(self):
        """Сторож хоткеев. Детектор живёт в пульс-хуке (см. _pulse) — ему
        нечего «лечить»: он либо получает события, либо нет.

        Единственный режим отказа — смерть низкоуровневого хука (некоторые
        игры/оверлеи выкидывают чужие хуки из цепочки). Он виден по полной
        тишине клавиатуры >20 минут — хук переустанавливается.
        """
        import keyboard
        self._hk_last_seen = time.time()
        try:
            keyboard.hook(self._pulse)
        except Exception:
            log.exception("не удалось поставить пульс-хук клавиатуры")

        def watchdog():
            while True:
                time.sleep(300)
                try:
                    if time.time() - self._hk_last_seen > 1200:
                        self._hk_last_seen = time.time()
                        log.warning("клавиатура молчит 20+ минут — "
                                    "переустанавливаю пульс-хук")
                        with self._hk_lock:
                            keyboard.unhook_all()
                            keyboard.hook(self._pulse)
                            self._pressed.clear()
                            self._fired.clear()
                    # RegisterHotKey-канал не «снимается» системой, но поток
                    # может умереть — проверяем живость и перезапускаем
                    if not self._hk_disp.is_alive():
                        log.warning("RegisterHotKey-диспетчер не работает — "
                                    "перезапуск")
                        with self._hk_lock:
                            self._hk_disp.update(list(self._hk_reg_fn))
                except Exception:
                    log.exception("сбой сторожа хоткеев")

        threading.Thread(target=watchdog, name="sosna-hk-watchdog", daemon=True).start()

    def _on_reg_hotkey(self, combo_str: str):
        """WM_HOTKEY от RegisterHotKey-диспетчера (основной канал)."""
        fn = self._hk_reg_fn.get(combo_str)
        if fn is None:
            return
        log.info("хоткей %s → %s (RegisterHotKey)",
                 combo_str, getattr(fn, "__name__", "?"))
        self._on_hotkey(fn, combo_str)

    def _pulse(self, event=None):
        """Каждое событие клавиатуры в системе (дёшево, микросекунды).

        Здесь же — детектор хоткеев: свой, вместо add_hotkey, чей
        последовательный автомат (порядок нажатий + таймаут 1с между
        шагами + подавляющий хук на середине комбинации) не работает
        в играх: зажатые WASD сбрасывают шаги, заранее зажатые
        модификаторы не дают ему стартовать, а игровые клавиши
        проглатываются. Наш детектор основан на множестве зажатых
        скан-кодов: порядок, скорость и удерживаемые клавиши не важны.
        """
        try:
            self._hk_last_seen = time.time()
            if event is None:
                return
            sc = getattr(event, "scan_code", None)
            if sc is None:
                return
            codes = (sc,) if isinstance(sc, int) else tuple(sc)
            if event.event_type == "down":
                self._pressed.update(codes)
            else:
                self._pressed.difference_update(codes)
            if self._hotkeys_suspended or not self._hk_map:
                return
            fired_now = []
            for parts, combo_str, fn in self._hk_map:
                hit = all(any(c in self._pressed for c in part) for part in parts)
                if hit:
                    if combo_str not in self._fired:   # один раз на каждое зажатие
                        self._fired.add(combo_str)
                        fired_now.append((combo_str, fn))
                else:
                    self._fired.discard(combo_str)     # отпустили — снова armed
            for combo_str, fn in fired_now:
                log.info("хоткей %s → %s (LL)", combo_str,
                         getattr(fn, "__name__", "?"))
                self._on_hotkey(fn, combo_str)
        except Exception:
            # библиотека глотает исключения хуков через print_exc (под
            # pythonw — в никуда), поэтому логируем сами
            log.exception("сбой детектора хоткеев")

    # ---------- перевод фрагмента по хоткею ----------

    def start_capture(self):
        if self._translating:
            log.debug("перевод уже идёт — пропуск нажатия")
            return
        self._translating = True

        def worker():
            try:
                self._do_capture()
            except Exception as e:  # noqa: BLE001
                log.exception("перевод фрагмента не удался")
                self.ui_schedule(lambda: result_window.show_message(
                    self.root, f"Сосна: ошибка — {e}", 6))
            finally:
                self._translating = False

        threading.Thread(target=worker, name="sosna-capture", daemon=True).start()

    def _select_region(self, screenshot):
        """Выделение области в UI-потоке; ждём с таймаутом (защита от зависания)."""
        done = threading.Event()
        box = {}

        def select():
            try:
                box["value"] = selector.select_region(self.root, screenshot)
            except Exception:
                log.exception("оверлей выделения упал")
            finally:
                done.set()

        self.ui_schedule(select)
        if not done.wait(timeout=900):
            log.error("выделение области не завершилось за 15 минут")
            return None
        return box.get("value")

    def _do_capture(self, bbox=None):
        """Перевод фрагмента. bbox=None — спросить область выделением."""
        t0 = time.time()
        # мгновенно снимаем весь экран, пока контекстные меню не исчезли
        with sosna_log.stage("grab-screen", log=log):
            screenshot = selector.grab_screen()

        if bbox is None:
            with sosna_log.stage("select-region", log=log):
                bbox = self._select_region(screenshot)
        if not bbox:
            return

        cfg = config.load()
        vx, vy, _, _ = selector.virtual_screen()
        img = screenshot.crop((
            bbox[0] - vx, bbox[1] - vy,
            bbox[0] - vx + bbox[2], bbox[1] - vy + bbox[3],
        ))
        # язык: «Авто» — умное определение (алфавит + 1 уточняющий проход)
        if cfg.get("ocr_lang", "auto") == "auto":
            lang_tag, text = ocr.detect_lang(img)
            src = ocr.nllb_src_for(lang_tag, text, cfg.get("nllb_src", "eng_Latn"))
        else:
            lang_tag, text = cfg["ocr_lang"], ocr.recognize(img, cfg["ocr_lang"])
            src = cfg.get("nllb_src", "eng_Latn")
        self._last_capture = {"bbox": bbox, "lang_tag": lang_tag, "src": src}
        if not text or ocr.is_garbage(text, lang_tag):
            log.info("фрагмент: связный текст не найден (язык %s)", lang_tag)
            self.ui_schedule(lambda: result_window.show_message(
                self.root, "Сосна: связный текст на фрагменте не найден", 4))
            return

        with sosna_log.stage("translate", log=log):
            translated = translate.smart_translate_one(text, src, cfg["nllb_tgt"])
        history.record(text, translated, f"{src}→{cfg['nllb_tgt']}")
        log.info("фрагмент переведён за %.2fs", time.time() - t0)
        self.ui_schedule(lambda: self._show_result(
            translated, text, bbox, cfg, src))

    def _show_result(self, translated, original, bbox, cfg, src=""):
        if self._result_win is not None and self._result_win.winfo_exists():
            self._result_win.destroy()
        tgt = cfg.get("nllb_tgt", "rus_Cyrl")

        def on_retranslate(new_text, src_arg, tgt_arg):
            """Повторный перевод исправленного текста (кнопка «✏️ Исправить»)."""
            out = translate.smart_translate_one(new_text, src_arg, tgt_arg)
            history.record(new_text, out, f"{src_arg}→{tgt_arg}")
            return out

        self._result_win = result_window.show_result(
            self.root, translated, original, bbox,
            cfg.get("result_timeout_sec", 60), src=src, tgt=tgt,
            on_retranslate=on_retranslate,
        )

    # ---------- повтор последнего перевода ----------

    def repeat_last(self):
        """Перевести ту же область, что и в прошлый раз (без выделения)."""
        if self._translating:
            return
        last = self._last_capture
        if not last:
            log.info("повтор: предыдущего перевода ещё не было")
            self.ui_schedule(lambda: result_window.show_message(
                self.root, "Сосна: предыдущего перевода ещё не было", 4))
            return
        self._translating = True

        def worker():
            try:
                self._do_capture(bbox=last["bbox"])
            except Exception as e:  # noqa: BLE001
                log.exception("повторный перевод не удался")
                self.ui_schedule(lambda: result_window.show_message(
                    self.root, f"Сосна: ошибка — {e}", 6))
            finally:
                self._translating = False

        threading.Thread(target=worker, name="sosna-repeat", daemon=True).start()

    # ---------- перевод всего экрана ----------

    def start_fullscreen(self):
        if self._translating:
            return
        self._translating = True

        def worker():
            try:
                self._do_fullscreen()
            except Exception as e:  # noqa: BLE001
                log.exception("перевод экрана не удался")
                self.ui_schedule(lambda: result_window.show_message(
                    self.root, f"Сосна: ошибка — {e}", 6))
            finally:
                self._translating = False

        threading.Thread(target=worker, name="sosna-fullscreen", daemon=True).start()

    def _do_fullscreen(self):
        t0 = time.time()
        cfg = config.load()
        with sosna_log.stage("grab-screen", log=log):
            screenshot = selector.grab_screen()

        # оверлей показываем мгновенно, переводы добавляем постепенно
        shown = threading.Event()
        overlay = {}

        def show():
            try:
                overlay["obj"] = screen_view.show_translated(
                    self.root, screenshot, schedule=self.ui_schedule)
            except Exception:
                log.exception("оверлей перевода экрана не открылся")
            finally:
                shown.set()

        self.ui_schedule(show)
        shown.wait(timeout=30)
        ov = overlay.get("obj")
        if ov is None:
            return

        if cfg.get("ocr_lang", "auto") == "auto":
            lang_tag, _ = ocr.detect_lang(screenshot)
            src = ocr.nllb_src_for(lang_tag, "", cfg.get("nllb_src", "eng_Latn"))
        else:
            lang_tag, src = cfg["ocr_lang"], cfg.get("nllb_src", "eng_Latn")

        with sosna_log.stage("ocr-blocks", log=log):
            blocks = ocr.recognize_blocks(screenshot, lang_tag)
        # фильтр контента: ссылки, символы и текст уже на целевом языке
        # не переводим (никаких «русского на русский» и плашек над URL)
        before = len(blocks)
        blocks = [b for b in blocks
                  if ocr.should_translate_block(b["text"], lang_tag, cfg["nllb_tgt"])]
        if before != len(blocks):
            log.info("перевод экрана: отфильтровано %d блоков (мусор/целевой язык)",
                     before - len(blocks))
        if ov.is_closed():
            return
        if not blocks:
            log.info("перевод экрана: текст не найден")
            ov.finish(0)
            return
        log.info("перевод экрана: %d блоков, движок %s", len(blocks), cfg["engine"])
        ov.set_total(len(blocks))

        # модель удерживаем на все пачки — без циклов загрузки/выгрузки
        translate.hold()
        chunk = 8
        try:
            for i in range(0, len(blocks), chunk):
                if ov.is_closed():
                    return
                part = blocks[i:i + chunk]
                trs = translate.smart_translate(
                    [b["text"] for b in part], src, cfg["nllb_tgt"])
                for b, t in zip(part, trs):
                    b["tr"] = t
                ov.add_blocks(part)
        finally:
            translate.release()
        ov.finish(len(blocks))
        log.info("перевод экрана завершён за %.1fs", time.time() - t0)

    # ---------- прогрев движка ----------

    def warmup(self):
        if translate.is_ready():
            result_window.show_message(self.root, "Сосна: движок уже прогрет ⚡", 4)
            return
        if not translate.model_present():
            result_window.show_message(
                self.root, "Сосна: база этого движка не скачана — «Обновить базы».", 6)
            return
        result_window.show_message(self.root, "Сосна: прогреваю движок...", 4)

        def worker():
            translate.preload()
            ok = translate.wait_ready(timeout=180)
            if ok:
                msg = "Сосна: движок прогрет ⚡ перевод готов к работе"
            else:
                err = translate.get_error()
                msg = f"Сосна: не удалось прогреть движок — {err}" if err \
                    else "Сосна: не удалось прогреть движок (см. лог)"
            self.ui_schedule(lambda: result_window.show_message(self.root, msg, 6))

        threading.Thread(target=worker, name="sosna-warmup", daemon=True).start()

    # ---------- живые субтитры ----------

    def toggle_live(self):
        if self._live is not None and self._live.is_active():
            self._live.stop()
            return
        if self._live_booting:
            log.debug("live уже запускается — пропуск нажатия")
            return
        if self._translating:
            return
        self._live_booting = True

        def worker():
            try:
                session = live.start_live(
                    self.root,
                    on_closed=lambda: setattr(self, "_live", None),
                    schedule=self.ui_schedule)
                if session is None:
                    return
                self._live = session
            except Exception as e:  # noqa: BLE001
                log.exception("живые субтитры не запустились")
                self.ui_schedule(lambda: result_window.show_message(
                    self.root, f"Сосна: ошибка — {e}", 6))
            finally:
                self._live_booting = False

        threading.Thread(target=worker, name="sosna-live-boot", daemon=True).start()

    # ---------- хоткей / трей / настройки ----------

    def register_hotkey(self):
        with self._hk_lock:
            self._register_hotkey_locked()

    def _register_hotkey_locked(self):
        """Собрать карту хоткеев и раздать её обоим каналам.

        Под _hk_lock. RegisterHotKey — основной канал (системная гарантия
        доставки, метод NVIDIA); LL-детектор в _pulse — резерв для
        эксклюзивного фулскрина и занятых комбинаций. Дедупликация —
        по метке времени WM_HOTKEY в _pulse.
        """
        import keyboard
        cfg = config.load()
        combos = [
            (cfg["hotkey"], self.start_capture),
            (cfg.get("hotkey_fullscreen", ""), self.start_fullscreen),
            (cfg.get("hotkey_live", ""), self.toggle_live),
            (cfg.get("hotkey_repeat", ""), self.repeat_last),
        ]
        self._hk_map = []
        self._hk_reg_fn = {}
        registered = []
        for combo, fn in combos:
            combo = (combo or "").strip().lower()
            if not combo:
                continue
            try:
                parts = [set(keyboard.key_to_scan_codes(p))
                         for p in combo.split("+") if p.strip()]
            except Exception as e:
                # пользователь увидит честное сообщение, а не молчание
                log.error("хоткей «%s» не распознан: %s", combo, e)
                self.ui_schedule(lambda c=combo, err=e: result_window.show_message(
                    self.root, f"Сосна: хоткей {c} не назначен — {err}", 8))
                continue
            self._hk_map.append((parts, combo, fn))
            self._hk_reg_fn[combo] = fn
            registered.append(combo)
        self._fired.clear()
        self._hk_fired_at.clear()
        # асинхронно перерегистрирует основной канал; LL-канал уже обновлён
        self._hk_disp.update(registered)
        if registered:
            log.info("хоткеи активны: %s (RegisterHotKey + LL-резерв)",
                     ", ".join(registered))

    def open_settings(self):
        for w in self.root.winfo_children():
            if isinstance(w, tk.Toplevel) and w.title() == "Сосна — настройки":
                w.lift(); w.focus_force()
                return w
        return settings_window.open_settings(
            self.root,
            on_saved=lambda cfg: self.register_hotkey(),
            suspend_hotkeys=self.suspend_hotkeys,
            resume_hotkeys=self.resume_hotkeys,
        )

    def manual_update(self):
        """Ручное обновление баз из трея: фон + уведомление о результате."""

        def worker():
            try:
                msg, changed = updater.update()
            except Exception as e:  # noqa: BLE001
                log.exception("ручное обновление не удалось")
                msg, changed = f"Ошибка обновления: {e}", False
            if changed:
                translate.reload()
            self.ui_schedule(lambda: result_window.show_message(
                self.root, f"Сосна: {msg}", 8))

        threading.Thread(target=worker, name="sosna-update-manual", daemon=True).start()

    # ---------- самодиагностика ----------

    def _self_check(self):
        """Стартовая проверка здоровья: модели, языки OCR, хоткеи — в лог."""
        try:
            log.info("самодиагностика:")
            for name, spec in config.ENGINES.items():
                log.info("  движок %s: %s", spec["short"],
                         "установлен" if translate.engine_present(name)
                         else "НЕ установлен")
            try:
                langs = ocr.available_languages()
                log.info("  языки OCR: %s", ", ".join(langs) if langs else "НЕТ!")
                if not langs:
                    self.ui_schedule(lambda: result_window.show_message(
                        self.root, "Сосна: языки распознавания не установлены — "
                                   "добавь язык в Параметры Windows.", 12))
            except Exception as e:
                log.error("  языки OCR не получены: %s", e)
            if self._hk_map:
                log.info("  хоткеи: %d активных", len(self._hk_map))
            else:
                log.warning("  хоткеи НЕ зарегистрированы!")
                self.ui_schedule(lambda: result_window.show_message(
                    self.root, "Сосна: горячие клавиши не назначены — "
                               "проверьте настройки.", 10))
            log.info("  данные: %s", config.DATA_DIR)
            log.info("  память процесса: %d МБ", sosna_log.mem_mb())
        except Exception:
            log.exception("самодиагностика упала")

    # ---------- цикл приложения ----------

    def _updater_status(self, msg):
        """Статус автообновления: в лог; большие события — уведомлением в трей."""
        log.info("автообновление: %s", msg)
        try:
            if self.tray is not None and (
                    "Скачиваю" in msg or "скачана" in msg or "ошибка" in msg.lower()):
                self.tray.notify(msg, "Сосна")
        except Exception:
            log.debug("уведомление трея не показано", exc_info=True)

    def run(self):
        # иконка для всех окон приложения (настройки, история, глоссарий)
        try:
            from PIL import ImageTk
            self._icon_photo = ImageTk.PhotoImage(make_icon_image())
            self.root.iconphoto(True, self._icon_photo)
        except Exception:
            log.warning("иконка приложения не создана", exc_info=True)
        cfg = config.load()
        if cfg.get("model_in_ram", True):
            translate.preload()
        else:
            idle = int(cfg.get("model_idle_min", 5))
            if idle > 0:
                self.root.after(1500, lambda: result_window.show_message(
                    self.root,
                    f"Сосна: база загрузится при первом переводе и останется "
                    f"в памяти {idle} мин.", 6))
        self.register_hotkey()
        self.root.after(50, self._poll_ui)     # разгрузчик UI-очереди
        self.root.after(80, self._poll_hotkeys)  # разгрузчик очереди хоткеев
        self.start_hotkey_watchdog()           # сторож: хоткеи живут, пока запущена Сосна
        self._start_tray()
        self._auto_updater = updater.AutoUpdater(status_cb=self._updater_status)
        self._auto_updater.start()

        # если модель не скачана — предложить обновить базы сразу
        if not translate.model_present():
            self.root.after(1000, lambda: result_window.show_message(
                self.root, "Сосна: база переводчика не скачана.\n"
                           "Откройте трей → Настройки → «Обновить базы».", 15))

        sosna_log.start_mem_logger(60)
        self.root.after(3000, self._self_check)
        try:
            self.root.mainloop()
        finally:
            self._cleanup()

    def _start_tray(self):
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem(
                lambda item: f"Перевести фрагмент ({config.load()['hotkey']})",
                lambda: self.ui_schedule(self.start_capture), default=True),
            pystray.MenuItem(
                lambda item: "🔁 Повторить последний ({})".format(
                    config.load().get("hotkey_repeat") or "не задан"),
                lambda: self.ui_schedule(self.repeat_last)),
            pystray.MenuItem(
                lambda item: "Перевести весь экран ({})".format(
                    config.load().get("hotkey_fullscreen") or "не задан"),
                lambda: self.ui_schedule(self.start_fullscreen)),
            pystray.MenuItem(
                "🔥 Прогреть движок",
                lambda: self.ui_schedule(self.warmup)),
            pystray.MenuItem(
                lambda item: ("⏹ Остановить субтитры" if self._live is not None and self._live.is_active()
                              else "📺 Живые субтитры ({})".format(
                                  config.load().get("hotkey_live") or "не задан")),
                lambda: self.ui_schedule(self.toggle_live)),
            pystray.MenuItem(
                "История переводов",
                lambda: self.ui_schedule(lambda: history.open_history(self.root))),
            pystray.MenuItem(
                "📖 Глоссарий",
                lambda: self.ui_schedule(lambda: glossary.open_editor(self.root))),
            pystray.MenuItem("Настройки", lambda: self.ui_schedule(self.open_settings)),
            pystray.MenuItem("Обновить базы", lambda: self.ui_schedule(self.manual_update)),
            pystray.MenuItem(
                "💚 Поддержать",
                lambda: self.ui_schedule(self.open_donate)),
            pystray.MenuItem("Выход", lambda: self.ui_schedule(self.quit)),
        )
        self.tray = pystray.Icon(
            "sosna", make_icon_image(),
            f"Сосна v{config.APP_VERSION} — офлайн-переводчик", menu
        )
        self.tray.run_detached()

    def open_donate(self):
        import webbrowser
        webbrowser.open(DONATE_URL)

    def _cleanup(self):
        """Аккуратный выход: снять хуки, остановить потоки, выгрузить модель."""
        log.info("завершение работы...")
        try:
            if self._live is not None and self._live.is_active():
                self._live.stop()
        except Exception:
            log.exception("остановка live-сессии при выходе")
        try:
            if self._auto_updater is not None:
                self._auto_updater.stop()
        except Exception:
            pass
        try:
            self._hk_disp.stop()   # RegisterHotKey-канал: выйти из GetMessage
        except Exception:
            log.exception("остановка диспетчера хоткеев")
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            log.exception("снятие хуков клавиатуры")
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        try:
            translate.unload()
        except Exception:
            pass
        log.info("Сосна остановлена. Память в конце: %d МБ", sosna_log.mem_mb())

    def quit(self):
        self.root.quit()


def main():
    if not _acquire_single_instance():
        log.warning("второй экземпляр заблокирован (уже запущен)")
        ctypes.windll.user32.MessageBoxW(
            0, "Сосна уже запущена — ищите значок 🌲 в трее.\n"
               "(Это окно можно закрыть.)", "Сосна", 0x40)
        return
    app = SosnaApp()
    try:
        app.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
