"""Живые субтитры: непрерывный перевод выбранной области экрана.

⚡ Быстрый режим: пиксель-diff до OCR (неизменённый кадр = цикл бесплатный),
   перевод каждой новой строки сразу. Задержка ~0.5–1.5с.
🏆 Качественный режим: стабилизация — строка переводится, только если
   распозналась одинаково два цикла подряд (защита от дрожания OCR,
   приём LunaTranslator). Задержка ~2–3с, точность выше. Статичный текст
   тоже переводится: неизменный кадр считается подтверждением.

📝 Фраза целиком (live_wait_sentence, по умолчанию вкл.): перевод
   дожидается знака конца предложения (. ! ? …) — цельная фраза даёт
   переводчику контекст, перевод не «прыгает» на каждом слове. Если
   пунктации нет 6 секунд — переводим как есть (фолбэк). Число строк
   в окне — настройка live_lines (по умолчанию 1).

Сессия удерживает модель переводчика в памяти (translate.hold) — без
циклов «загрузка/выгрузка» на каждую строку. Окно закрывается только
из UI-потока (через schedule-колбэк приложения).

Потокобезопасность: рабочий цикл кладёт события в очередь, окно обновляет
только UI-поток (опросник _poll). Фазы: prepare() в фоне → open_window()
в UI-потоке.
"""
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

import config
import ocr
import selector
import sosna_log
import translate
import ui_kit

log = sosna_log.get()

# тема живого окна (тёмная): фон/рамка в цветах тёмной темы настроек
_BG = "#1b1e23"
_BORDER = "#3a3f46"
_GREEN = "#8bc34a"

# кэш Tk-шрифтов: создание шрифта — дорогой Tcl-вызов, кэш убирает лаги
_FONTS = {}


def _font(size: int):
    f = _FONTS.get(size)
    if f is None:
        f = _FONTS[size] = tkfont.Font(family="Segoe UI", size=size, weight="bold")
    return f


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


# Знаки конца предложения: дожидаемся их перед переводом (лучший контекст).
# Приём стриминговых субтитров: незаконченная фраза не переводится, пока
# не появится точка/вопрос/восклицание — иначе перевод прыгает и теряет смысл.
_SENTENCE_END = ".!?…。！？‼⁉"


def _is_sentence_end(text: str) -> bool:
    t = (text or "").rstrip()
    return bool(t) and t[-1] in _SENTENCE_END


# сколько секунд ждать знака конца, прежде чем перевести фразу как есть
# (фолбэк для субтитров вообще без пунктуации; таймер НЕ сбрасывается
# ростом фразы — иначе OCR-джиттер мог откладывать перевод вечно)
_SENTENCE_TIMEOUT = 4.0
# фраза моложе этого возраста при замене считается OCR-шумом, а не страницей
_PENDING_MIN_AGE = 1.5
# после стольких подряд несовпадающих чтений стабилизация сдаётся и
# отдаёт фразу гейту (иначе джиттер на видео замораживает сессию)
_STAB_MAX_RESETS = 3


def _signature(img) -> bytes:
    """Подпись кадра: ч/б миниатюра шириной 192px с сохранением пропорций.

    Сравнение — с допуском (_sig_changed): строгое равенство байтов
    сглаживало смену 1–2 букв в мелких строках, и субтитры пропускались."""
    w, h = img.size
    tw = 192
    th = max(1, round(h * tw / max(w, 1)))
    return img.convert("L").resize((tw, th)).tobytes()


_SIG_TOLERANCE = 12  # разница яркоты пикселя, выше которой кадр «изменился»


def _sig_changed(prev: bytes | None, cur: bytes) -> bool:
    """Изменился ли кадр: любой пиксель ярче/темнее допуска = изменение."""
    if prev is None or len(prev) != len(cur):
        return True
    for a, b in zip(prev, cur):
        if a > b + _SIG_TOLERANCE or b > a + _SIG_TOLERANCE:
            return True
    return False


class LiveSession:
    """Один запуск живых субтитров: окно + фоновый цикл."""

    def __init__(self, root, bbox, schedule=None):
        self.root = root
        self.bbox = bbox
        self._schedule = schedule      # маршалинг в UI-поток (main.ui_schedule)
        self.stop_event = threading.Event()
        self.cache = {}                # нормализованный текст -> перевод
        self.prev_sig = None
        self.last_norm = ""
        self._stab_norm = ""
        self._stab_text = ""
        self._stab_count = 0
        self._stab_resets = 0          # подряд несовпадающих чтений (джиттер)
        self._pending = None           # (norm, text, ts) — фраза ждёт знака конца
        self._log = []                 # (время, оригинал, перевод) — до 20 фраз
        self.log_win = None            # окно «Контекст» с последними переводами
        self._log_body = None
        self._log_shown = -1
        self.on_closed = None          # вызывается в UI-потоке при остановке
        self.win = None
        self.lines_var = None
        self._q = queue.Queue()        # события цикла -> UI
        self.lang_tag = "en-US"
        self.src = "eng_Latn"
        self._holding_model = False

    # ---------- фаза 1: подготовка (фоновый поток) ----------

    def prepare(self):
        cfg = config.load()
        first = ocr.capture_region(self.bbox)
        if cfg.get("ocr_lang", "auto") == "auto":
            lang_tag, text = ocr.detect_lang(first)
            self.src = ocr.nllb_src_for(lang_tag, text, cfg.get("nllb_src", "eng_Latn"))
        else:
            lang_tag = cfg["ocr_lang"]
            self.src = cfg.get("nllb_src", "eng_Latn")
        self.lang_tag = lang_tag
        # модель удерживается до конца сессии — живые субтитры не платят
        # за загрузку базы на каждой новой строке
        translate.hold()
        self._holding_model = True
        log.info("live-сессия готова: язык=%s src=%s bbox=%s", lang_tag, self.src, self.bbox)

    # ---------- фаза 2: окно (только UI-поток) ----------

    def open_window(self):
        vx, vy, vw, vh = selector.virtual_screen()
        x, y, w, h = self.bbox
        width = max(360, min(640, w))
        win = tk.Toplevel(self.root)
        win.title("Сосна — живые субтитры")
        win.overrideredirect(True)                # без рамки Windows
        win.attributes("-topmost", True)
        win.attributes("-alpha", 1.0)             # по умолчанию НЕ прозрачное
        win.configure(bg=_BG, highlightthickness=1,
                      highlightbackground=_BORDER)  # рамка цвета тёмной темы

        head = tk.Frame(win, bg=_BG)
        head.pack(fill="x")
        tk.Label(head, text="📺 Живые субтитры", bg=_BG, fg=_GREEN,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(8, 0), pady=2)

        # кнопка прозрачности: ◑ полупрозрачно / ■ непрозрачно (по умолчанию)
        alpha = {"on": False}

        def toggle_alpha():
            alpha["on"] = not alpha["on"]
            win.attributes("-alpha", 0.55 if alpha["on"] else 1.0)
            alpha_btn.configure(text="■" if alpha["on"] else "◑")

        # порядок упаковки справа: ✕ упакована ПЕРВОЙ — оказывается
        # в самом углу; кнопки крупные круглые с подсказкой при наведении
        ui_kit.round_button(head, "✕", "Остановить субтитры и закрыть",
                            self.stop, kind="danger").pack(side="right",
                                                           padx=(2, 8))
        ui_kit.round_button(head, "🕘", "Контекст: последние переводы",
                            self.open_log_window).pack(side="right", padx=4)
        alpha_btn = ui_kit.round_button(head, "◑", "Прозрачность вкл/выкл",
                                        toggle_alpha)
        alpha_btn.pack(side="right", padx=4)

        self.lines_var = tk.StringVar(value="…")
        self._history = []  # недавние строки: при растяжении окна возвращаются
        self._last_size = (0, 0)
        self._render_scheduled = False
        self.text_label = tk.Label(win, textvariable=self.lines_var, bg=_BG,
                                   fg="#ffffff", font=("Segoe UI", 13, "bold"),
                                   wraplength=width - 16, justify="left",
                                   anchor="nw")  # строка сразу под шапкой
        self.text_label.pack(fill="both", expand=True, padx=8, pady=(2, 8))

        # ресайз за грип (рамки Windows нет — тянем нижний правый угол сами)
        grip = tk.Label(win, text="◢", bg=_BG, fg=_GREEN,
                        cursor="bottom_right_corner", font=("Segoe UI", 12, "bold"))
        grip.place(relx=1.0, rely=1.0, anchor="se")
        rstate = {"x": 0, "y": 0, "w": 0, "h": 0}

        def grip_press(e):
            rstate.update(x=e.x_root, y=e.y_root,
                          w=win.winfo_width(), h=win.winfo_height())

        def grip_drag(e):
            nw = max(300, min(900, rstate["w"] + e.x_root - rstate["x"]))
            nh = max(110, min(600, rstate["h"] + e.y_root - rstate["y"]))
            win.geometry(f"{nw}x{nh}")

        grip.bind("<ButtonPress-1>", grip_press)
        grip.bind("<B1-Motion>", grip_drag)

        def on_resize(e):
            # перерисовка только при изменении РАЗМЕРА окна (перемещение — мимо),
            # и не чаще раза в 60 мс: иначе перетаскивание дико лагает
            if (e.width, e.height) == self._last_size:
                return
            self._last_size = (e.width, e.height)
            if not self._render_scheduled:
                self._render_scheduled = True
                win.after(60, self._render_soon)

        win.bind("<Configure>", on_resize)

        # позиция: под выделенной областью, прижата к экрану
        wx = max(vx + 4, min(x + w // 2 - width // 2, vx + vw - width - 4))
        wy = y + h + 8
        win.update_idletasks()
        wh = max(win.winfo_reqheight(), 120)  # запас под пару строк сразу
        if wy + wh > vy + vh:  # не влезает снизу — покажем над областью
            wy = max(vy + 4, y - wh - 8)
        win.geometry(f"{width}x{wh}+{wx}+{wy}")

        # перетаскивание за заголовок
        drag = {"dx": 0, "dy": 0}
        head.bind("<ButtonPress-1>", lambda e: drag.update(dx=e.x, dy=e.y))
        head.bind("<B1-Motion>", lambda e: win.geometry(
            f"+{win.winfo_x() + e.x - drag['dx']}+{win.winfo_y() + e.y - drag['dy']}"))

        self.win = win
        # перерисовать, когда окно реально отображено и размеры известны
        win.after_idle(self._render)
        win.after(120, self._render)
        self._poll()

    # ---- подгонка текста под размер окна ----

    _MIN_SIZE = 9
    _MAX_SIZE = 30

    def _chrome_h(self) -> int:
        """Сколько в окне занимает всё, кроме области текста (шапка, отступы)."""
        if self.win is None:
            return 40
        label_h = max(self.text_label.winfo_height(), 10)
        return max(36, self.win.winfo_height() - label_h)

    def _fit(self, lines: list, inner_w: int, inner_h: int) -> int:
        """Кегль (вплоть до MIN-1), при котором строки влезают; MIN-1 = не влезает."""
        for size in range(self._MAX_SIZE, self._MIN_SIZE - 1, -1):
            f = _font(size)
            rows = sum(max(1, math.ceil(f.measure(ln) / inner_w)) for ln in lines)
            if rows * f.metrics("linespace") <= inner_h:
                return size
        return self._MIN_SIZE - 1

    def _lines_to_show(self) -> int:
        """Сколько последних фраз показывать в окне (настройка live_lines)."""
        try:
            return max(1, min(3, int(config.load().get("live_lines", 1))))
        except (TypeError, ValueError):
            return 1

    def _render_soon(self):
        self._render_scheduled = False
        self._render()

    def _render(self):
        """Показать историю строк, подобрав кегль под фактическую область текста.

        Не влезает даже в минимальный кегль — окно дорастает: «кармана»,
        прячущего текст, не бывает. При растяжении окна старые строки
        возвращаются из истории.
        """
        win = self.win
        if win is None or not win.winfo_exists():
            return
        inner_w = max(140, win.winfo_width() - 16)
        inner_h = max(36, win.winfo_height() - self._chrome_h())

        pick_n = self._lines_to_show()
        pick = self._history[-pick_n:]
        chosen, size = pick[-1:] if pick else ["…"], self._MIN_SIZE
        for k in range(min(pick_n, len(pick)), 0, -1):
            cand = pick[-k:]
            s = self._fit(cand, inner_w, inner_h)
            if s >= self._MIN_SIZE:
                chosen, size = cand, s
                break

        f = _font(size)
        rows = sum(max(1, math.ceil(f.measure(ln) / inner_w)) for ln in chosen)
        text_h = rows * f.metrics("linespace")
        if text_h > inner_h:  # тесно даже минимальным — окно дорастает
            needed = min(600, text_h + self._chrome_h() + 6)
            win.geometry(f"{win.winfo_width()}x{needed}")

        self.lines_var.set("\n".join(chosen))
        self.text_label.configure(
            font=("Segoe UI", size, "bold"), wraplength=inner_w,
        )

    # ---------- фаза 3: рабочий цикл (фоновый поток) ----------

    def spawn_loop(self):
        threading.Thread(target=self._loop, name="sosna-live", daemon=True).start()

    def stop(self):
        """Мгновенно остановить сессию и закрыть окно (безопасно из любого потока).

        Окно_destroy только в UI-потоке — через schedule-колбэк приложения.
        """
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self._holding_model:
            translate.release()   # снимаем удержание модели (можно из любого потока)
            self._holding_model = False

        def close():
            if self.win is not None and self.win.winfo_exists():
                self.win.destroy()
            self.win = None
            if self.on_closed:
                cb, self.on_closed = self.on_closed, None
                cb()

        if threading.current_thread() is threading.main_thread():
            close()
        elif self._schedule is not None:
            self._schedule(close)
        else:
            # резервный путь без schedule — только после падения mainloop
            try:
                self.root.after(0, close)
            except RuntimeError:
                log.error("не удалось закрыть окно live-сессии: mainloop мёртв")

    def is_active(self) -> bool:
        return not self.stop_event.is_set()

    def _loop(self):
        cfg = config.load()
        try:
            interval = max(0.5, float(cfg.get("live_interval", 2)))
        except (TypeError, ValueError):
            interval = 2.0
        quality = cfg.get("live_mode", "fast") == "quality"
        wait_sentence = bool(cfg.get("live_wait_sentence", True))
        tgt = cfg.get("nllb_tgt", "rus_Cyrl")
        log.info("live-цикл запущен: интервал=%.1fs режим=%s фраза-целиком=%s",
                 interval, "quality" if quality else "fast",
                 "да" if wait_sentence else "нет")
        err_count = 0

        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                self._cycle(quality, tgt, wait_sentence)
                err_count = 0
            except Exception:
                # один сбой не останавливает сессию, но и не глотается молча:
                # первые 3 ошибки — в лог, дальше только счётчик
                err_count += 1
                if err_count <= 3:
                    log.exception("сбой live-цикла #%d", err_count)
                elif err_count % 50 == 0:
                    log.error("live-цикл: %d-й сбой подряд", err_count)
            spent = time.time() - t0
            self.stop_event.wait(max(0.2, interval - spent))

        log.info("live-цикл остановлен")

    def _gate_commit(self, text: str, norm: str, wait_sentence: bool) -> list:
        """Решить, какие фразы фиксируются (переводятся) сейчас. 0–2 элемента.

        Стратегия из работ о прямом переводе речи (simultaneous speech
        translation): сегмент фиксируется по любому из событий —
          - конец предложения (точка/?/!) — перевод сразу;
          - текст ЗАМЕНИЛ ждущий (не его продолжение) — ждущий фиксируется
            (commit-on-replace): страница субтитров не теряется, даже когда
            диктор говорит без остановки;
          - текст ВЫРОС из ждущего (префикс) — диктор продолжает, копим;
          - таймаут без пунктуации — фиксируем как есть.
        Замена раньше _PENDING_MIN_AGE секунд — OCR-шум, старое отбрасываем.
        """
        if not wait_sentence:
            self._pending = None
            return [(text, norm)]
        now = time.time()
        ends = _is_sentence_end(text)
        page, pnorm, pts = self._pending if self._pending else (None, None, 0.0)
        self._pending = None

        if pnorm is not None:
            if pnorm == norm:
                # та же фраза: ждём дозревания (таймер не сбрасываем)
                if ends or now - pts >= _SENTENCE_TIMEOUT:
                    return [(text, norm)]
                self._pending = (text, norm, pts)
                return []
            grown = norm.startswith(pnorm) and len(norm) > len(pnorm)
            if grown and not ends:
                # накопление речи: копим. Таймер НЕ перезапускаем —
                # краевой шум OCR («lea» у края региона) иначе мог бы
                # откладывать перевод бесконечно; фраза уйдёт в перевод
                # через _SENTENCE_TIMEOUT после первого появления
                self._pending = (text, norm, pts)
                return []
            commits = []
            # страница субтитров сменилась (или фраза завершена):
            # старую фиксируем — иначе диктор «проглатывает» перевод
            if now - pts >= _PENDING_MIN_AGE:
                commits.append((page, pnorm))
            if ends:
                commits.append((text, norm))
            else:
                self._pending = (text, norm, now)
            return commits

        if ends:
            return [(text, norm)]
        self._pending = (text, norm, now)
        return []

    def _commit_translate(self, text: str, norm: str, tgt: str):
        """Зафиксировать сегмент: перевести (или взять из кэша) и показать."""
        self.last_norm = norm
        if norm in self.cache:
            tr = self.cache[norm]     # повтор строки — мгновенно из кэша
        else:
            tr = translate.smart_translate_one(text, self.src, tgt)
            if len(self.cache) >= 100:
                # выкидываем самые старые (раньше кэш чистился целиком —
                # циклические субтитры начинались заново с задержкой)
                for k in list(self.cache)[:20]:
                    del self.cache[k]
            self.cache[norm] = tr
        self._log.append((time.strftime("%H:%M:%S"), text, tr))
        if len(self._log) > 20:
            del self._log[:len(self._log) - 20]
        self._q.put(("lines", text, tr))

    def _cycle(self, quality: bool, tgt: str, wait_sentence: bool = True):
        img = ocr.capture_region(self.bbox)
        sig = _signature(img)
        if not _sig_changed(self.prev_sig, sig):
            # Кадр не изменился — OCR не нужен. Но в режиме качества
            # статичный кадр ПОДТВЕРЖДАЕТ отложенную строку, а ждущая
            # знака конца фраза с истёкшим таймаутом уходит в перевод
            # даже на замороженном кадре.
            if (quality and self._stab_count == 1 and self._stab_norm
                    and self._stab_norm != self.last_norm):
                text, norm = self._stab_text, self._stab_norm
            elif (self._pending is not None
                    and time.time() - self._pending[2] >= _SENTENCE_TIMEOUT):
                page, pnorm, _pts = self._pending
                self._pending = None
                log.info("live: фраза без знака конца %.0fs — перевожу как есть",
                         time.time() - _pts)
                self._commit_translate(page, pnorm, tgt)
                return
            else:
                return
        else:
            self.prev_sig = sig
            text = ocr.recognize(img, self.lang_tag)
            norm = _normalize(text)
            if not norm or norm == self.last_norm:
                self._stab_norm, self._stab_text, self._stab_count = "", "", 0
                self._stab_resets = 0
                return
            if ocr.is_garbage(text, self.lang_tag):
                return  # белиберду не переводим — ждём связный текст

            if quality:
                growing = (self._stab_norm and norm.startswith(self._stab_norm)
                           and len(norm) > len(self._stab_norm))
                if growing:
                    self._stab_resets = 0
                    pass  # речь накапливается — стабилизация не нужна,
                          # гейт сам копит и зафиксирует по событию
                elif norm != self._stab_norm:
                    self._stab_norm = norm
                    self._stab_text = text
                    self._stab_count = 1
                    self._stab_resets += 1
                    if self._stab_resets < _STAB_MAX_RESETS:
                        return
                    # джиттер не даёт получить два одинаковых чтения
                    # подряд (видео за текстом) — сдаёмся и отдаём
                    # текущее чтение гейту: сессия не должна замереть
                    self._stab_resets = 0
                    log.debug("live: стабилизация не сошлась за %d чтений — "
                              "пропускаю", _STAB_MAX_RESETS)
                else:
                    self._stab_resets = 0
                    self._stab_count += 1
                    if self._stab_count < 2:
                        return

        # фиксация сегментов: по точке / смене страницы / таймауту
        for ctext, cnorm in self._gate_commit(text, norm, wait_sentence):
            self._commit_translate(ctext, cnorm, tgt)

    # ---------- UI-поток: опросник событий ----------

    def _poll(self):
        if self.win is None or not self.win.winfo_exists():
            return
        try:
            while True:
                kind, a, b = self._q.get_nowait()
                if kind == "lines":
                    self._push(b)
        except queue.Empty:
            pass
        # опросник живёт, пока существует окно (и после stop — до его закрытия)
        if self.win is not None and self.win.winfo_exists():
            self.win.after(100, self._poll)

    def _push(self, tr: str):
        self._history = (self._history + ["⮞ " + tr])[-6:]
        self._render()  # максимум 3 последние строки влезающего объёма

    # ---------- окно «Контекст»: 20 последних переводов ----------

    def open_log_window(self):
        """Отдельное окно с 20 последними переводами сессии (оригинал +
        перевод) — чтобы видеть контекст того, что говорит диктор."""
        if self.log_win is not None and self.log_win.winfo_exists():
            self.log_win.lift()
            self.log_win.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.title("Сосна — контекст субтитров")
        win.overrideredirect(True)     # без рамки Windows — как окно субтитров
        win.attributes("-topmost", True)
        win.configure(bg=_BG, highlightthickness=1,
                      highlightbackground=_BORDER)
        head = tk.Frame(win, bg=_BG)
        head.pack(fill="x", pady=(6, 2))
        title_lbl = tk.Label(head, text="🕘 Последние переводы (новые снизу)",
                             bg=_BG, fg=_GREEN, font=("Segoe UI", 10, "bold"),
                             anchor="w")
        title_lbl.pack(side="left", fill="x", expand=True, padx=(10, 0))
        # ✕ упакована первой справа — самая крайняя в углу
        ui_kit.round_button(head, "✕", "Закрыть окно контекста",
                            win.destroy, kind="danger").pack(side="right",
                                                             padx=(2, 8))
        # рамки нет — окно перетаскивается за шапку (и за заголовок)
        _drag = {"dx": 0, "dy": 0}

        def _drag_start(e):
            _drag.update(dx=e.x, dy=e.y)

        def _drag_move(e):
            win.geometry(f"+{win.winfo_x() + e.x - _drag['dx']}"
                         f"+{win.winfo_y() + e.y - _drag['dy']}")

        head.bind("<ButtonPress-1>", _drag_start)
        head.bind("<B1-Motion>", _drag_move)
        title_lbl.bind("<ButtonPress-1>", _drag_start)
        title_lbl.bind("<B1-Motion>", _drag_move)
        body = tk.Text(win, bg="#15181c", fg="#ffffff", relief="flat",
                       font=("Segoe UI", 10), wrap="word", padx=10, pady=8,
                       state="disabled", width=64, height=18)
        body.pack(fill="both", expand=True, padx=10)
        body.tag_configure("ts", foreground="#6a7480",
                           font=("Segoe UI", 8))
        body.tag_configure("tr", foreground="#ffffff",
                           font=("Segoe UI", 11, "bold"))
        body.tag_configure("src", foreground="#8a919b",
                           font=("Segoe UI", 9))
        btns = tk.Frame(win, bg=_BG)
        btns.pack(fill="x", padx=10, pady=(4, 10))

        def copy_all():
            win.clipboard_clear()
            win.clipboard_append("\n".join(
                f"[{ts}] {tr}\n   {src}" for ts, src, tr in self._log))

        tk.Button(btns, text="📋 Копировать всё", command=copy_all,
                  bg="#2e7d32", fg="#ffffff", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=10).pack(side="left")
        tk.Label(btns, text="двойной клик по строке — скопировать перевод",
                 bg=_BG, fg="#6a7480", font=("Segoe UI", 8)).pack(
            side="left", padx=8)

        def copy_line(event):
            try:
                idx = body.index(f"@{event.x},{event.y}").split(".")[0]
                line_no = int(idx)
            except Exception:
                return
            # каждая запись занимает 3 строки: время+перевод / оригинал / пустая
            block_no = (line_no - 1) // 3
            if 0 <= block_no < len(self._log):
                win.clipboard_clear()
                win.clipboard_append(self._log[block_no][2])

        body.bind("<Double-Button-1>", copy_line)

        # позиция: правее окна субтитров, прижата к экрану
        try:
            if self.win is not None and self.win.winfo_exists():
                import selector as _sel
                vx, vy, vw, vh = _sel.virtual_screen()
                win.update_idletasks()
                x = self.win.winfo_x() + self.win.winfo_width() + 12
                y = self.win.winfo_y()
                x = min(x, vx + vw - win.winfo_reqwidth() - 8)
                win.geometry(f"+{max(vx + 4, x)}+{max(vy + 4, y)}")
        except Exception:
            pass

        self.log_win = win
        self._log_body = body
        self._log_shown = -1   # форсировать первую отрисовку
        self._poll_log()

    def _poll_log(self):
        """Обновлять окно контекста, пока оно открыто (раз в 0.5с)."""
        win = self.log_win
        if win is None or not win.winfo_exists():
            self.log_win = None
            self._log_body = None
            return
        body = self._log_body
        if body is not None and len(self._log) != self._log_shown:
            self._log_shown = len(self._log)
            body.configure(state="normal")
            body.delete("1.0", "end")
            # хронологический порядок: старые сверху, новые снизу (как чат)
            for ts, src, tr in self._log:
                body.insert("end", ts + "  ", "ts")
                body.insert("end", tr + "\n", "tr")
                body.insert("end", "   " + src.replace("\n", " ") + "\n\n", "src")
            body.see("end")   # автопрокрутка к самой новой фразе
            body.configure(state="disabled")
        win.after(500, self._poll_log)


def start_live(root, on_closed=None, schedule=None) -> LiveSession | None:
    """Выбрать область (по замороженному снимку) и запустить сессию.

    Вызывается из фонового потока: блокируется на выделении, окно строит
    через schedule (очередь, опрашиваемая UI-потоком приложения).
    Возвращает сессию или None, если пользователь отменил выделение.
    """
    screenshot = selector.grab_screen()
    done = threading.Event()
    box = {}

    def select():
        try:
            box["value"] = selector.select_region(root, screenshot)
        finally:
            done.set()

    _dispatch = schedule or (lambda fn: root.after(0, fn))
    _dispatch(select)
    if not done.wait(timeout=900):   # зависшее выделение не вешает поток навсегда
        log.error("выделение области live не завершилось за 15 минут")
        return None
    bbox = box.get("value")
    if not bbox:
        return None

    session = LiveSession(root, bbox, schedule=schedule)
    session.on_closed = on_closed

    def run():
        try:
            session.prepare()
        except Exception:
            log.exception("подготовка live-сессии не удалась")
            session.stop()
            return
        _dispatch(lambda: (session.open_window(), session.spawn_loop()))

    threading.Thread(target=run, name="sosna-live-boot", daemon=True).start()
    return session
