"""Окно настроек «Сосны»: карточки, профили, движки, онлайн, живой режим, темы.

Исправлено: закрытие окна во время записи хоткея больше не отключает
глобальные хоткеи до перезапуска; числа из Spinbox валидируются (мусорный
ввод — сообщение, а не молчаливый сбой); профиль реально применяет пресет
к форме; тяжёлые операции с моделью уходят в фон (UI не замирает);
обновление баз честно репортит прогресс в UI-поток.
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import config
import sosna_log
import ui_kit
import updater
from ui_kit import Tooltip  # общая подсказка (реэкспорт для совместимости)

log = sosna_log.get()


def _mk_theme(name):
    """Словарь темы из токенов ui_kit (единый источник цветов и градиентов)."""
    p = ui_kit.palette(name)
    return dict(win=p["grad_top"], card=p["card"], border=p["card_rim"],
                title=p["title"], text=p["text"], hint=p["hint"],
                select=p["select"], field=p["field"], btn=p["btn"],
                btn_text=p["btn_text"], green=p["btn_green"], ok=p["ok"],
                err=p["err"], combo_bg=p["combo_bg"], accent=p["accent"],
                blue=p["blue"], grad_top=p["grad_top"],
                grad_bottom=p["grad_bottom"])


THEMES = {n: _mk_theme(n) for n in ("light", "dark", "komi")}


def _center(win, parent=None):
    """Центр виртуального рабочего стола на любом разрешении."""
    win.update_idletasks()
    import selector
    vx, vy, vw, vh = selector.virtual_screen()
    w = win.winfo_width() or win.winfo_reqwidth()
    h = win.winfo_height() or win.winfo_reqheight()
    x = vx + max(0, (vw - w) // 2)
    y = vy + max(0, (vh - h) // 2)
    win.geometry(f"+{x}+{y}")


class Tooltip(ui_kit.Tooltip):  # совместимость: реализация в ui_kit
    pass


def _parse_int(var, lo, hi, default, win):
    """Число из Spinbox/Entry с валидацией. Возвращает None при ошибке."""
    raw = (var.get() or "").strip()
    try:
        val = int(float(raw)) if raw else default
    except ValueError:
        messagebox.showwarning(
            "Сосна", f"«{raw}» — не число. Введите значение от {lo} до {hi}.",
            parent=win)
        return None
    return max(lo, min(hi, val))


def open_settings(parent, on_saved=None, suspend_hotkeys=None, resume_hotkeys=None):
    cfg = config.load()
    T = THEMES.get(cfg.get("ui_theme", "komi"), THEMES["komi"])
    BG_CARD = T["card"]

    win = tk.Toplevel(parent)
    win.title(f"Сосна {config.APP_VERSION} — настройки")
    win.resizable(True, True)  # можно растягивать мышью за край
    win.minsize(700, 380)
    win.configure(bg=T["win"])

    # ttk-стили под тему (иначе ttk-виджеты остаются системными светлыми)
    style = ttk.Style(win)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(".", background=T["card"], foreground=T["text"], fieldbackground=T["field"])
    style.configure("TFrame", background=T["card"])
    style.configure("TLabel", background=T["card"], foreground=T["text"])
    style.configure("TEntry", fieldbackground=T["field"], foreground=T["text"],
                    insertcolor=T["text"])
    style.configure("TCombobox", fieldbackground=T["field"], foreground=T["text"],
                    background=T["combo_bg"], arrowcolor=T["text"])
    style.configure("TCheckbutton", background=T["card"], foreground=T["text"])
    style.configure("TSpinbox", fieldbackground=T["field"], foreground=T["text"],
                    background=T["card"], arrowcolor=T["text"])
    style.map("TCombobox",
              fieldbackground=[("readonly", T["field"])],
              foreground=[("readonly", T["text"])])

    # прокручиваемый контейнер: карточки лежат прямо на canvas — сквозь
    # промежутки виден градиент, а тени рисуются одним фоновым изображением
    import selector as _sel
    _vx, _vy, _vw, _vh = _sel.virtual_screen()
    WIN_W = 860
    MAX_H = int(_vh * 0.75)
    PAD_X = 14
    CARD_W = WIN_W - PAD_X * 2

    canvas = tk.Canvas(win, bg=T["grad_top"], highlightthickness=0,
                       width=WIN_W, height=100)
    vbar = tk.Scrollbar(win, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    vbar.pack(side="right", fill="y")
    win.bind("<MouseWheel>", lambda e: canvas.yview_scroll(
        -1 if e.delta > 0 else 1, "units"))

    _cards = []   # фреймы-карточки: по ним считаются позиции и тени

    def card(title):
        """Карточка-элевация: тональная поверхность, мягкая тень на фоне."""
        c = tk.Frame(win, bg=T["card"], highlightthickness=1,
                     highlightbackground=T["border"])
        head = tk.Frame(c, bg=T["card"])
        head.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(head, text=title, bg=T["card"], fg=T["title"],
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        inner = ttk.Frame(c, padding=(14, 2, 14, 12))
        inner.pack(fill="x")
        _cards.append(c)
        return inner

    def hint(parent, text):
        lbl = tk.Label(parent, text=text, bg=BG_CARD, fg=T["hint"],
                       font=("Segoe UI", 8), justify="left", anchor="w")
        lbl.pack(fill="x", pady=(2, 0))
        # перенос по словам под фактическую ширину поля — текст всегда виден
        lbl.bind("<Configure>", lambda e, _l=lbl: _l.configure(
            wraplength=max(120, e.width - 2)))
        return lbl

    # ---------- ⚡ Профиль и оформление ----------
    c1 = card("⚡ Профиль и оформление")
    tooltips = []  # всплывающие подсказки для длинных значений
    profile_var = tk.StringVar(value=cfg.get("profile", "balance"))

    def _profile_picked():
        """Профиль сразу подставляет свой пресет в форму (обещание из подсказки)."""
        preset = config.PROFILES.get(profile_var.get(), {}).get("values", {})
        if "engine" in preset:
            engine_var.set(config.ENGINES[preset["engine"]]["label"])
        if "model_in_ram" in preset:
            ram_var.set(preset["model_in_ram"])
        if "live_mode" in preset:
            live_var.set(LIVE_LABELS[preset["live_mode"]])
        if "live_interval" in preset:
            interval_var.set(str(preset["live_interval"]))

    prow = tk.Frame(c1, bg=BG_CARD)
    prow.pack(fill="x")
    for key, spec in config.PROFILES.items():
        tk.Radiobutton(prow, text=spec["label"], variable=profile_var, value=key,
                       bg=BG_CARD, fg=T["text"], selectcolor=T["select"],
                       activebackground=BG_CARD, command=_profile_picked,
                       font=("Segoe UI", 10)).pack(side="left", padx=(0, 14))
    hint(c1, "💻 Слабый ПК — очень быстро, среднее качество, память почти не тратится "
             "(ноутбук с 4–8 ГБ). ⚖️ Баланс — средняя скорость, хорошее качество, "
             "база в памяти ~1 ГБ (обычный ПК). 🚀 Мощный ПК — медленно, отличное "
             "качество, база ~2 ГБ, точные субтитры (игровой ПК). Профиль сразу "
             "подставляет свои значения в форму — можно поправить и сохранить.")
    theme_row = tk.Frame(c1, bg=BG_CARD)
    theme_row.pack(fill="x", pady=(8, 0))
    THEME_LABELS = {"komi": "🌲 Коми", "light": "☀️ Светлая",
                    "dark": "🌙 Тёмная"}
    theme_var = tk.StringVar(
        value=THEME_LABELS.get(cfg.get("ui_theme", "komi"),
                               THEME_LABELS["komi"]))
    ttk.Label(theme_row, text="Интерфейс:").pack(side="left")
    theme_box = ttk.Combobox(theme_row, textvariable=theme_var, state="readonly",
                             width=14, values=list(THEME_LABELS.values()))
    theme_box.pack(side="left", padx=8)
    tooltips.append(Tooltip(theme_box, lambda: theme_var.get()))
    hint(c1, "Тема применяется после «Сохранить» — окно настроек откроется заново.")

    # ---------- ⌨️ Горячие клавиши ----------
    c2 = card("⌨️ Горячие клавиши")
    grid2 = ttk.Frame(c2)
    grid2.pack(fill="x")
    hk_status = tk.StringVar(value="")
    cap_q = queue.Queue()
    capturing = {"on": False}

    def _resume_if_capturing():
        """Вернуть хоткеи, если окно закрыли во время записи комбинации.

        Раньше suspend остаётся навсегда — все хоткеи приложения умирали
        до перезапуска."""
        if capturing["on"]:
            capturing["on"] = False
            if resume_hotkeys:
                resume_hotkeys()
            log.info("запись хоткея прервана закрытием окна — хоткеи возвращены")

    win.protocol("WM_DELETE_WINDOW",
                 lambda: (_resume_if_capturing(), win.destroy()))

    def _capture(var, btn):
        """Записать комбинацию: пользователь нажимает клавиши — они биндятся."""
        if capturing["on"]:
            return
        capturing["on"] = True
        old = btn.cget("text")
        btn.configure(text="…нажимайте")
        hk_status.set("Нажмите нужную комбинацию клавиш… (Esc — отмена)")
        if suspend_hotkeys:
            suspend_hotkeys()
        import keyboard as _kb

        def worker():
            try:
                combo = _kb.read_hotkey(suppress=False)
            except Exception:
                combo = ""
            cap_q.put((var, combo or "", btn, old))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_capture():
        try:
            var, combo, btn, old = cap_q.get_nowait()
        except queue.Empty:
            pass
        else:
            capturing["on"] = False
            if resume_hotkeys:
                resume_hotkeys()
            btn.configure(text=old)
            if combo and combo != "esc":
                var.set(combo)
                hk_status.set(f"Записано: {combo}")
            else:
                hk_status.set("Запись отменена")
        if win.winfo_exists():
            win.after(90, _poll_capture)

    def hotkey_row(row, label, var):
        ttk.Label(grid2, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(grid2, textvariable=var, width=18).grid(row=row, column=1, sticky="w", padx=8)
        btn = tk.Button(grid2, text="🔑 Записать", relief="flat", bg=T["btn"],
                        fg=T["btn_text"], font=("Segoe UI", 9), padx=8,
                        command=lambda: _capture(var, btn))
        btn.grid(row=row, column=2, sticky="w")

    hotkey_var = tk.StringVar(value=cfg["hotkey"])
    hotkey_fs_var = tk.StringVar(value=cfg.get("hotkey_fullscreen", ""))
    hotkey_live_var = tk.StringVar(value=cfg.get("hotkey_live", ""))
    hotkey_rep_var = tk.StringVar(value=cfg.get("hotkey_repeat", ""))
    hotkey_row(0, "Перевести фрагмент:", hotkey_var)
    hotkey_row(1, "Перевести весь экран:", hotkey_fs_var)
    hotkey_row(2, "Живые субтитры:", hotkey_live_var)
    hotkey_row(3, "Повторить последний перевод:", hotkey_rep_var)
    tk.Label(c2, textvariable=hk_status, bg=BG_CARD, fg=T["ok"],
             font=("Segoe UI", 8)).pack(anchor="w")
    hint(c2, "Можно вписать комбинацию вручную (формат keyboard: ctrl+alt+t) или нажать "
             "«🔑 Записать» и нажать клавиши на клавиатуре — они забиндятся сами. "
             "Пустое поле — хоткей выключен.")
    _poll_capture()

    # ---------- 👁 Распознавание и перевод ----------
    c3 = card("👁 Распознавание и перевод")
    grid3 = ttk.Frame(c3)
    grid3.pack(fill="x")
    try:
        import ocr
        ocr_langs = ocr.available_languages()
    except Exception:
        ocr_langs = [cfg["ocr_lang"]]
    OCR_AUTO_LABEL = "Авто (определять)"
    ocr_choices = [OCR_AUTO_LABEL] + ocr_langs
    current = OCR_AUTO_LABEL if cfg.get("ocr_lang", "auto") == "auto" else cfg["ocr_lang"]
    if current not in ocr_choices:
        ocr_choices.append(current)
    ttk.Label(grid3, text="Язык распознавания:").grid(row=0, column=0, sticky="w", pady=3)
    ocr_var = tk.StringVar(value=current)
    ttk.Combobox(grid3, textvariable=ocr_var, values=ocr_choices,
                 state="readonly", width=30).grid(row=0, column=1, sticky="w", padx=8)
    ttk.Label(grid3, text="Переводить на:").grid(row=1, column=0, sticky="w", pady=3)
    tgt_var = tk.StringVar(value=cfg["nllb_tgt"])
    ttk.Combobox(grid3, textvariable=tgt_var, state="readonly", width=30,
                 values=["rus_Cyrl (русский)", "ukr_Cyrl (украинский)",
                         "eng_Latn (английский)"]).grid(row=1, column=1, sticky="w", padx=8)
    ttk.Label(grid3, text="Движок перевода:").grid(row=2, column=0, sticky="nw", pady=3)
    engine_names = list(config.ENGINES)
    engine_default = cfg.get("engine", "nllb-600M")
    if engine_default not in config.ENGINES:
        engine_default = "nllb-600M"  # защита от кривого значения в конфиге
    engine_var = tk.StringVar(value=config.ENGINES[engine_default]["label"])
    engine_box = ttk.Combobox(grid3, textvariable=engine_var, state="readonly", width=56,
                              values=[config.ENGINES[n]["label"] for n in engine_names])
    engine_box.grid(row=2, column=1, sticky="w", padx=8)
    tooltips.append(Tooltip(engine_box, lambda: engine_var.get()))
    ram_var = tk.BooleanVar(value=cfg.get("model_in_ram", True))
    ttk.Checkbutton(grid3, text="Держать базу в оперативной памяти постоянно",
                    variable=ram_var).grid(row=3, column=0, columnspan=2, sticky="w",
                                           pady=(6, 0))
    idle_row = ttk.Frame(grid3)
    idle_row.grid(row=4, column=0, columnspan=2, sticky="w")
    ttk.Label(idle_row, text="Если выключено — держать в памяти после перевода, мин:").pack(
        side="left", pady=(4, 0))
    idle_var = tk.StringVar(value=str(cfg.get("model_idle_min", 5)))
    ttk.Spinbox(idle_row, textvariable=idle_var, from_=0, to=240,
                width=5).pack(side="left", padx=6)
    hint(c3, "⚡ Opus-MT — быстро, среднее качество (~0.3 ГБ, свободная лицензия); "
             "🌲 NLLB — средне, хорошее (~1 ГБ); 🏆 NLLB-1.3B — медленно, отличное (~2 ГБ). "
             "Без галочки база грузится при первом переводе и остаётся в памяти "
             "указанное число минут (0 — выгружать сразу после перевода); "
             "живые субтитры удерживают базу всё время сессии.")

    # ---------- 🌐 Онлайн и глоссарий ----------
    c4 = card("🌐 Онлайн и глоссарий")
    grid4 = ttk.Frame(c4)
    grid4.pack(fill="x")
    ttk.Label(grid4, text="Онлайн-движок:").grid(row=0, column=0, sticky="w", pady=3)
    ONLINE_LABELS = {"off": "Выключен (только офлайн)",
                     "google": "Google (бесплатный)",
                     "mymemory": "MyMemory (бесплатный, лимит в день)",
                     "libretranslate": "LibreTranslate (открытый)"}
    online_var = tk.StringVar(
        value=ONLINE_LABELS.get(cfg.get("online_engine", "off"), ONLINE_LABELS["off"]))
    online_box = ttk.Combobox(grid4, textvariable=online_var, state="readonly", width=38,
                              values=list(ONLINE_LABELS.values()))
    online_box.grid(row=0, column=1, sticky="w", padx=8)
    tooltips.append(Tooltip(online_box, lambda: online_var.get()))
    ttk.Label(grid4, text="Как использовать:").grid(row=1, column=0, sticky="w", pady=3)
    MODE_LABELS = {"fallback": "Офлайн-первый — онлайн только при сбое",
                   "boost": "Улучшать онлайн-результатом (интернет нужен)",
                   "online": "Только онлайн (быстро, но без интернета не работает)"}
    mode_var = tk.StringVar(
        value=MODE_LABELS.get(cfg.get("online_mode", "fallback"), MODE_LABELS["fallback"]))
    mode_box = ttk.Combobox(grid4, textvariable=mode_var, state="readonly", width=38,
                            values=list(MODE_LABELS.values()))
    mode_box.grid(row=1, column=1, sticky="w", padx=8)
    tooltips.append(Tooltip(mode_box, lambda: mode_var.get()))
    btn_gloss = tk.Button(grid4, text="📖 Глоссарий терминов…", relief="flat",
                          bg=T["btn"], fg=T["btn_text"], font=("Segoe UI", 9), padx=10,
                          command=lambda: __import__("glossary").open_editor(win))
    btn_gloss.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
    hint(c4, "Онлайн-движки бесплатные и работают без ключей. «Улучшать» = перевод "
             "перепроверяется онлайн и заменяется лучшим вариантом. Глоссарий применяет "
             "твои термины к любому переводу.")

    # ---------- 📺 Живые субтитры ----------
    c5 = card("📺 Живые субтитры")
    grid5 = ttk.Frame(c5)
    grid5.pack(fill="x")
    ttk.Label(grid5, text="Режим:").grid(row=0, column=0, sticky="w", pady=3)
    LIVE_LABELS = {"fast": "⚡ Быстрый (задержка ~1с, средняя точность)",
                   "quality": "🏆 Качественный (задержка ~2–3с, стабильная точность)"}
    live_var = tk.StringVar(
        value=LIVE_LABELS.get(cfg.get("live_mode", "fast"), LIVE_LABELS["fast"]))
    live_box = ttk.Combobox(grid5, textvariable=live_var, state="readonly", width=52,
                            values=list(LIVE_LABELS.values()))
    live_box.grid(row=0, column=1, sticky="w", padx=8)
    tooltips.append(Tooltip(live_box, lambda: live_var.get()))
    ttk.Label(grid5, text="Цикл, сек:").grid(row=1, column=0, sticky="w", pady=3)
    interval_var = tk.StringVar(value=str(cfg.get("live_interval", 2)))
    ttk.Spinbox(grid5, textvariable=interval_var, from_=1, to=10, width=5).grid(
        row=1, column=1, sticky="w", padx=8)
    sentence_var = tk.BooleanVar(value=cfg.get("live_wait_sentence", True))
    ttk.Checkbutton(grid5,
                    text="Переводить фразу целиком — дожидаться точки/знака вопроса",
                    variable=sentence_var).grid(row=2, column=0, columnspan=2,
                                                sticky="w", pady=(6, 0))
    lines_row = ttk.Frame(grid5)
    lines_row.grid(row=3, column=0, columnspan=2, sticky="w")
    ttk.Label(lines_row, text="Строк перевода в окне:").pack(side="left",
                                                             pady=(4, 0))
    lines_var = tk.StringVar(value=str(cfg.get("live_lines", 1)))
    ttk.Spinbox(lines_row, textvariable=lines_var, from_=1, to=3,
                width=4).pack(side="left", padx=6)
    hint(c5, "Выбираешь область (например, где субтитры) — новые фразы переводятся сами. "
             "«Фраза целиком»: перевод ждёт конца предложения — цельная фраза даёт "
             "нейросети контекст, перевод точнее и не прыгает; если пунктуации нет "
             "6 секунд, фраза переводится как есть. Число строк — сколько последних "
             "фраз держать в окне.")

    # ---------- 🔄 Обновления ----------
    c6 = card("🔄 Обновления")
    grid6 = ttk.Frame(c6)
    grid6.pack(fill="x")
    auto_var = tk.BooleanVar(value=cfg["auto_update"])
    ttk.Checkbutton(grid6, text="Автообновление баз при подключении к интернету",
                    variable=auto_var).grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(grid6, text="Интервал проверки, часов:").grid(row=1, column=0, sticky="w", pady=3)
    hours_var = tk.StringVar(value=str(cfg["update_interval_hours"]))
    ttk.Spinbox(grid6, textvariable=hours_var, from_=1, to=168, width=6).grid(
        row=1, column=1, sticky="w", padx=8)
    ttk.Label(grid6, text="Автозакрытие перевода, сек (0 — не закрывать):").grid(
        row=2, column=0, sticky="w", pady=3)
    timeout_var = tk.StringVar(value=str(cfg["result_timeout_sec"]))
    ttk.Spinbox(grid6, textvariable=timeout_var, from_=0, to=600, width=6).grid(
        row=2, column=1, sticky="w", padx=8)

    status_var = tk.StringVar(value="")
    busy = {"flag": False}

    def set_busy(flag):
        busy["flag"] = flag
        st = "disabled" if flag else "normal"
        btn_update.configure(state=st)
        btn_check.configure(state=st)
        btn_save.configure(state=st)

    def _ui(fn):
        """Выполнить fn в UI-потоке, пока окно живо (для фоновых worker'ов)."""
        if win.winfo_exists():
            win.after(0, fn)

    def run_bg(fn):
        def worker():
            try:
                msg = fn()
                if isinstance(msg, tuple):
                    msg = msg[0]
                _ui(lambda: status_var.set(msg or "Готово"))
                _ui(refresh_engine_labels)
            except Exception as e:  # noqa: BLE001
                log.exception("фоновая задача настроек")
                _ui(lambda: status_var.set(f"Ошибка: {e}"))
            finally:
                _ui(lambda: set_busy(False))
        threading.Thread(target=worker, daemon=True).start()

    btn_row = tk.Frame(win, bg=T["card"], highlightthickness=1,
                       highlightbackground=T["border"])
    _cards.append(btn_row)
    btn_check = tk.Button(btn_row, text="Проверить обновления", relief="flat",
                          bg=T["btn"], fg=T["btn_text"], font=("Segoe UI", 9), padx=10,
                          command=lambda: (set_busy(True), status_var.set("Проверяю..."),
                                           run_bg(lambda: updater.check_for_updates())))
    btn_check.pack(side="left")
    btn_update = tk.Button(btn_row, text="⬇️ Обновить базы", relief="flat",
                           bg=T["green"], fg="#ffffff", font=("Segoe UI", 9, "bold"),
                           padx=10,
                           command=lambda: (set_busy(True),
                                            status_var.set("Скачиваю недостающие базы..."),
                                            run_bg(lambda: updater.update(
                                                progress=lambda m: _ui(
                                                    lambda m=m: status_var.set(m))))))
    btn_update.pack(side="left", padx=6)
    engine_status_labels = []

    def refresh_engine_labels():
        import translate as _tr
        for lbl, name in engine_status_labels:
            ok = _tr.engine_present(name)
            lbl.configure(text=(f"✓ {config.ENGINES[name]['short']}" if ok else
                                f"✗ {config.ENGINES[name]['short']} (нет базы)"),
                          fg=T["ok"] if ok else T["err"])

    for name in config.ENGINES:
        lbl = tk.Label(btn_row, text="", bg=btn_row.cget("bg"),
                       font=("Segoe UI", 9), cursor="hand2")
        lbl.pack(side="left", padx=4)
        engine_status_labels.append((lbl, name))
        tooltips.append(Tooltip(
            lbl,
            lambda n=name: config.ENGINES[n]["label"] +
            ("" if __import__("translate").engine_present(n)
             else "  —  база не скачана: нажмите «Обновить базы»")))
    refresh_engine_labels()
    tk.Label(btn_row, textvariable=status_var, bg=btn_row.cget("bg"),
             fg=T["ok"], font=("Segoe UI", 9), wraplength=300,
             justify="left").pack(side="left", padx=8)

    NLLB_BY_OCR = {
        "en-US": "eng_Latn", "en": "eng_Latn", "ru": "rus_Cyrl",
        "zh-CN": "zho_Hans", "zh-Hans": "zho_Hans", "zh": "zho_Hans",
        "uk": "ukr_Cyrl", "de": "deu_Latn", "fr": "fra_Latn",
        "es": "spa_Latn", "it": "ita_Latn", "pt": "por_Latn",
        "pl": "pol_Latn", "tr": "tur_Latn", "ja": "jpn_Jpan", "ko": "kor_Hang",
    }

    def _label_to_key(mapping, value, default):
        for k, v in mapping.items():
            if v == value:
                return k
        return default

    def _valid_hotkey(combo, field):
        """Строка хоткея парсится библиотекой keyboard и не дублируется."""
        if not combo:
            return True
        try:
            import keyboard
            keyboard.parse_hotkey(combo)
        except Exception as e:
            messagebox.showwarning(
                "Сосна", f"Комбинация «{combo}» в поле «{field}» не распознаётся:\n{e}",
                parent=win)
            return False
        return True

    def do_save():
        hotkey = hotkey_var.get().strip().lower()
        if not hotkey:
            messagebox.showwarning("Сосна", "Горячая клавиша не может быть пустой", parent=win)
            return
        # валидация чисел: мусорный ввод — сообщение, а не молчаливый сбой
        live_interval = _parse_int(interval_var, 1, 30, 2, win)
        if live_interval is None:
            return
        live_lines = _parse_int(lines_var, 1, 3, 1, win)
        if live_lines is None:
            return
        hours = _parse_int(hours_var, 1, 168, 24, win)
        if hours is None:
            return
        result_timeout = _parse_int(timeout_var, 0, 600, 0, win)
        if result_timeout is None:
            return
        idle_min = _parse_int(idle_var, 0, 240, 5, win)
        if idle_min is None:
            return

        hk_fs = hotkey_fs_var.get().strip().lower()
        hk_live = hotkey_live_var.get().strip().lower()
        hk_rep = hotkey_rep_var.get().strip().lower()
        for combo, field in ((hotkey, "Перевести фрагмент"),
                             (hk_fs, "Перевести весь экран"),
                             (hk_live, "Живые субтитры"),
                             (hk_rep, "Повторить последний перевод")):
            if not _valid_hotkey(combo, field):
                return
        seen = {}
        for combo, field in ((hotkey, "фрагмент"), (hk_fs, "весь экран"),
                             (hk_live, "субтитры"), (hk_rep, "повтор")):
            if combo:
                if combo in seen:
                    messagebox.showwarning(
                        "Сосна", f"Комбинация {combo} назначена дважды: "
                                 f"«{seen[combo]}» и «{field}».", parent=win)
                    return
                seen[combo] = field

        ocr_choice = ocr_var.get()
        ocr_lang = "auto" if ocr_choice.startswith("Авто") else ocr_choice
        engine = _label_to_key(
            {n: config.ENGINES[n]["label"] for n in engine_names},
            engine_var.get(), cfg.get("engine", "nllb-600M"))
        new_cfg = {
            **cfg,
            "profile": profile_var.get(),
            "ui_theme": _label_to_key(THEME_LABELS, theme_var.get(), "light"),
            "hotkey": hotkey,
            "hotkey_fullscreen": hk_fs,
            "hotkey_live": hk_live,
            "hotkey_repeat": hk_rep,
            "engine": engine,
            "ocr_lang": ocr_lang,
            "nllb_src": "eng_Latn" if ocr_lang == "auto" else NLLB_BY_OCR.get(ocr_lang, "eng_Latn"),
            "nllb_tgt": tgt_var.get().split()[0],
            "online_engine": _label_to_key(ONLINE_LABELS, online_var.get(), "off"),
            "online_mode": _label_to_key(MODE_LABELS, mode_var.get(), "fallback"),
            "live_mode": _label_to_key(LIVE_LABELS, live_var.get(), "fast"),
            "live_interval": live_interval,
            "live_lines": live_lines,
            "live_wait_sentence": sentence_var.get(),
            "auto_update": auto_var.get(),
            "update_interval_hours": hours,
            "result_timeout_sec": result_timeout,
            "model_in_ram": ram_var.get(),
            "model_idle_min": idle_min,
        }
        engine_changed = engine != cfg.get("engine")
        ram_changed = ram_var.get() != cfg.get("model_in_ram", True)
        config.save(new_cfg)
        if on_saved:
            on_saved(new_cfg)

        # тяжёлое (загрузка/выгрузка модели на секунды) — в фон, UI не замирает
        import translate

        def model_ops():
            try:
                if engine_changed:
                    translate.reload()
                elif new_cfg["model_in_ram"]:
                    translate.preload()
                elif ram_changed:
                    translate.unload()
            except Exception:
                log.exception("применение настроек модели")

        threading.Thread(target=model_ops, name="sosna-model-apply",
                         daemon=True).start()
        # окно НЕ закрываем — показываем подтверждение
        theme_changed = new_cfg["ui_theme"] != cfg.get("ui_theme", "light")
        save_status.set("✓ Настройки сохранены")
        win.after(3000, lambda: save_status.set(""))
        if theme_changed:
            _resume_if_capturing()
            win.destroy()
            win.after(250, lambda: open_settings(parent, on_saved, suspend_hotkeys,
                                                 resume_hotkeys))

    save_row = tk.Frame(win, bg=T["card"], highlightthickness=1,
                        highlightbackground=T["border"])
    _cards.append(save_row)
    save_status = tk.StringVar(value="")
    tk.Label(save_row, textvariable=save_status, bg=T["card"], fg=T["ok"],
             font=("Segoe UI", 10, "bold")).pack(side="left", padx=(14, 0),
                                                 pady=10)
    tk.Label(save_row, text=f"Сосна v{config.APP_VERSION}", bg=T["card"],
             fg=T["hint"], font=("Segoe UI", 8)).pack(side="right", padx=(0, 14))
    btn_save = tk.Button(save_row, text="💾 Сохранить", command=do_save, relief="flat",
                         bg=T["green"], fg="#ffffff", font=("Segoe UI", 10, "bold"),
                         padx=18, pady=4)
    btn_save.pack(side="right")

    _photo_ref = {}

    def _layout():
        """Разместить карточки на canvas и нарисовать градиент с тенями."""
        canvas.delete("content")
        win.update_idletasks()
        y = 6
        boxes = []
        for f in _cards:
            h = f.winfo_reqheight()
            canvas.create_window(PAD_X, y, anchor="nw", window=f,
                                 width=CARD_W, tags=("content",))
            boxes.append((PAD_X, y, CARD_W, h))
            y += h + 10
        total = y + 4
        _photo_ref["img"] = ui_kit.gradient_photo(
            canvas, max(WIN_W, canvas.winfo_width()), total, boxes, T)
        canvas.configure(scrollregion=(0, 0, WIN_W, total))
        return total

    total_h = _layout()
    win.geometry(f"{WIN_W}x{min(total_h + 2, MAX_H)}")
    _center(win)
    # страховка: после первой отрисовки шрифтов/эмодзи переложить заново
    win.after(120, lambda: (_layout(), _center(win))
              if win.winfo_exists() else None)
    win.after(400, lambda: (_layout(), _center(win))
              if win.winfo_exists() else None)
    win.focus_force()
    return win
