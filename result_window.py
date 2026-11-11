"""Окно результата: оригинал сверху, перевод ниже, исправление и переперевод.

Кнопка «✏️ Исправить» (идея ScreenTranslator): OCR ошибся — правишь
распознанный текст руками и переводишь заново, не снимая скриншот.
Цвета — из UI-кита (тема «Коми»).
"""
import threading
import tkinter as tk

import ui_kit

_P = ui_kit.palette("komi")


def show_result(parent, translated: str, original: str, bbox, timeout_sec: int = 60,
                src: str = "", tgt: str = "", on_retranslate=None):
    """Показать перевод во всплывающном окне рядом с выделенной областью.

    on_retranslate(text, src, tgt) -> str вызывается в фоне при повторном
    переводе исправленного текста (если передан).
    """
    left, top, w, h = bbox

    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg=_P["card"])

    pad = 12
    max_w = 480

    frame = tk.Frame(win, bg=_P["card"], bd=0, highlightthickness=1,
                     highlightbackground=_P["card_rim"])
    frame.pack(fill="both", expand=True, padx=1, pady=1)

    # явная красная ✕ в самом углу (клик по тексту и Esc тоже закрывают)
    ui_kit.round_button(frame, "✕", "Закрыть", lambda: win.destroy(),
                        kind="danger", size=26, font_size=11).place(
        relx=1.0, rely=0, x=-5, y=5, anchor="ne")

    title = tk.Label(
        frame, text="Сосна · перевод", bg=_P["card"], fg=_P["accent"],
        font=("Segoe UI", 9, "bold"), anchor="w",
    )
    title.pack(fill="x", padx=pad, pady=(pad - 2, 2))

    # оригинал — сверху, мелко
    src_lbl = tk.Label(
        frame, text=original, bg=_P["card"], fg=_P["hint"],
        font=("Segoe UI", 9), wraplength=max_w - 2 * pad, justify="left",
        anchor="nw",
    )
    if original:
        src_lbl.pack(fill="x", padx=pad, pady=2)

    # перевод — ниже, крупно
    body = tk.Label(
        frame, text=translated, bg=_P["card"], fg=_P["text"],
        font=("Segoe UI", 12), wraplength=max_w - 2 * pad, justify="left",
        anchor="nw",
    )
    body.pack(fill="x", padx=pad, pady=(2, 2))

    # кнопки копирования
    btns = tk.Frame(frame, bg=_P["card"])
    btns.pack(fill="x", padx=pad, pady=(0, pad - 2))

    cur = {"translated": translated, "original": original}

    def copy_to_clipboard(text, btn):
        win.clipboard_clear()
        win.clipboard_append(text)
        old = btn.cget("text")
        btn.configure(text="Скопировано ✓")
        btn.after(1200, lambda: btn.configure(text=old))

    btn_dst = tk.Button(
        btns, text="📋 Копировать перевод",
        command=lambda: copy_to_clipboard(cur["translated"], btn_dst),
        bg=_P["btn_green"], fg="#ffffff", relief="flat", bd=0,
        activebackground=_P["btn_green"], activeforeground="#ffffff",
        font=("Segoe UI", 9, "bold"), padx=10, pady=3, cursor="hand2",
    )
    btn_dst.pack(side="left")
    if original:
        btn_src = tk.Button(
            btns, text="Копировать оригинал",
            command=lambda: copy_to_clipboard(cur["original"], btn_src),
            bg=_P["btn"], fg=_P["btn_text"], relief="flat", bd=0,
            activebackground=_P["combo_bg"], activeforeground=_P["btn_text"],
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
        )
        btn_src.pack(side="left", padx=6)

    if on_retranslate and original:
        btn_edit = tk.Button(
            btns, text="✏️ Исправить", command=lambda: open_editor(),
            bg=_P["btn"], fg=_P["btn_text"], relief="flat", bd=0,
            activebackground=_P["combo_bg"], activeforeground=_P["btn_text"],
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
        )
        btn_edit.pack(side="left", padx=6)

        def open_editor():
            """Редактор распознанного текста с повторным переводом."""
            ed = tk.Toplevel(win)
            ed.title("Сосна — исправить текст")
            ed.attributes("-topmost", True)
            ed.configure(bg=_P["card"])
            tk.Label(ed, text="Правь распознанный текст и нажми «Перевести»:",
                     bg=_P["card"], fg=_P["hint"],
                     font=("Segoe UI", 9)).pack(anchor="w", padx=10, pady=(8, 2))
            txt = tk.Text(ed, bg=_P["field"], fg=_P["text"],
                          insertbackground=_P["text"], relief="flat", bd=0,
                          font=("Segoe UI", 11), width=52, height=6,
                          wrap="word", padx=8, pady=6)
            txt.pack(fill="both", expand=True, padx=10, pady=4)
            txt.insert("1.0", cur["original"])
            txt.focus_set()
            st = tk.Label(ed, text="", bg=_P["card"], fg=_P["accent"],
                          font=("Segoe UI", 9))
            st.pack(anchor="w", padx=10)

            def do_translate():
                new_text = txt.get("1.0", "end").strip()
                if not new_text:
                    return
                btn_go.configure(state="disabled", text="Перевожу…")
                st.configure(text="Перевожу заново…", fg=_P["hint"])

                def worker():
                    try:
                        result = on_retranslate(new_text, src_arg, tgt_arg)
                    except Exception as e:
                        result = None
                        err = str(e)
                    ed.after(0, lambda: done(new_text, result,
                                             None if result is not None else err))

                def done(new_original, result, err):
                    if not ed.winfo_exists():
                        return
                    if err is not None:
                        st.configure(text=f"Ошибка: {err}", fg=_P["err"])
                        btn_go.configure(state="normal", text="🔄 Перевести")
                        return
                    ed.destroy()
                    if not win.winfo_exists():
                        return
                    cur["original"] = new_original
                    cur["translated"] = result or ""
                    src_lbl.configure(text=cur["original"])
                    body.configure(text=cur["translated"])
                    # подгоняем размер окна под новый текст
                    win.update_idletasks()
                    _place(win, left, top, w)

                src_arg, tgt_arg = src, tgt
                row = tk.Frame(ed, bg=_P["card"])
                row.pack(fill="x", padx=10, pady=(2, 8))
                btn_go = tk.Button(row, text="🔄 Перевести", command=do_translate,
                                   bg=_P["btn_green"], fg="#ffffff", relief="flat",
                                   bd=0, activebackground=_P["btn_green"],
                                   activeforeground="#ffffff",
                                   font=("Segoe UI", 9, "bold"), padx=12, pady=3,
                                   cursor="hand2")
                btn_go.pack(side="left")
                ed.bind("<Escape>", lambda e: ed.destroy())
                ed.update_idletasks()
                ed.geometry(f"+{win.winfo_x() + 30}+{win.winfo_y() + 30}")

    # размер окна по содержимому
    def _place(win_, left_, top_, w_):
        win_.update_idletasks()
        ww = min(max(win_.winfo_reqwidth(), 260), max_w + 2 * pad)
        wh = min(win_.winfo_reqheight(), 560)
        # ставим окно правее-ниже выделения, прижимая к экрану
        import selector
        vx, vy, vw, vh = selector.virtual_screen()
        x = left_ + w_ + 16
        y = top_
        if x + ww > vx + vw:
            x = max(vx, left_ - ww - 16)
        # окно выше экрана (низкое разрешение) — прижимаем к верху
        y = min(max(vy, y), max(vy, vy + vh - wh)) if wh <= vh else vy
        win_.geometry(f"{ww}x{wh}+{x}+{y}")

    _place(win, left, top, w)

    def close(_event=None):
        if after_id:
            win.after_cancel(after_id)
        win.destroy()

    win.bind("<Escape>", close)
    for wdg in (frame, body, src_lbl, title):
        wdg.bind("<Button-1>", close)
        wdg.configure(cursor="hand2")

    after_id = None
    if timeout_sec:
        after_id = win.after(timeout_sec * 1000, close)

    win.focus_force()
    return win


def show_message(parent, text: str, timeout_sec: int = 5):
    """Короткое уведомление (ошибки, статусы) в углу экрана."""
    import selector
    vx, vy, vw, vh = selector.virtual_screen()

    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    lbl = tk.Label(
        win, text=text, bg=_P["card"], fg=_P["text"],
        font=("Segoe UI", 10), wraplength=420, justify="left", padx=14, pady=10,
    )
    lbl.pack()
    win.configure(bg=_P["card"], highlightthickness=1,
                  highlightbackground=_P["card_rim"])
    win.update_idletasks()
    ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
    win.geometry(f"{ww}x{wh}+{vx + vw - ww - 24}+{vy + vh - wh - 24}")

    def close(_event=None):
        win.destroy()

    win.bind("<Escape>", close)
    if timeout_sec:
        win.after(timeout_sec * 1000, close)
    return win
