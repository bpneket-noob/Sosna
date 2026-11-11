"""Глоссарий «Сосны»: пользовательский словарь терминов, применяемый после перевода.

Файл — в %APPDATA% (переживает переустановку), запись атомарная с .bak,
чтение кешируется по mtime (раньше файл перечитывался с диска на каждый
перевод). Границы термина — lookaround, а не \\b: термины с пунктуацией
(C++, Node.js) теперь работают.
"""
import os
import re
import threading

import config
import ui_kit

_P = ui_kit.palette("komi")

_PATH = os.path.join(config.DATA_DIR, "glossary.json")
_lock = threading.Lock()
_cache = {"mtime": None, "entries": None}


def _mtime():
    try:
        return os.path.getmtime(_PATH)
    except OSError:
        return None


def load() -> list:
    """Термины глоссария (кеш по mtime файла)."""
    mtime = _mtime()
    with _lock:
        if _cache["mtime"] == mtime and _cache["entries"] is not None:
            return _cache["entries"]
    entries = []
    data, _src = config.load_json_with_bak(_PATH)
    if isinstance(data, list):
        entries = [e for e in data if e.get("from") and e.get("to")]
    with _lock:
        _cache["mtime"] = mtime
        _cache["entries"] = entries
    return entries


def save(entries: list):
    with _lock:
        config.save_json_atomic(_PATH, entries, indent=1)
        _cache["mtime"] = _mtime()
        _cache["entries"] = [e for e in entries if e.get("from") and e.get("to")]


def add(from_term: str, to_term: str):
    entries = [e for e in load() if e["from"].lower() != from_term.strip().lower()]
    entries.insert(0, {"from": from_term.strip(), "to": to_term.strip()})
    save(entries)


def remove(from_term: str):
    save([e for e in load() if e["from"].lower() != from_term.strip().lower()])


def apply(text: str, entries: list | None = None) -> str:
    """Заменить термины глоссария в переводе (целиком слова, без регистра).

    Границы — через lookaround (?<!\\w)...(?!\\w): работает для терминов,
    начинающихся/кончающихся пунктуацией (C++, Node.js), в отличие от \\b.
    """
    entries = entries if entries is not None else load()
    for e in entries:
        pattern = re.compile(
            r"(?<!\w)" + re.escape(e["from"]) + r"(?!\w)", re.IGNORECASE)
        text = pattern.sub(e["to"], text)
    return text


def open_editor(parent):
    """Окно редактора глоссария: список терминов + добавление/удаление."""
    import tkinter as tk

    win = tk.Toplevel(parent)
    win.title("Сосна — глоссарий")
    win.geometry("520x420")
    win.configure(bg=_P["card"])

    tk.Label(win, text="Термины применяются к переводу: «engine → двигатель».",
             bg=_P["card"], fg=_P["hint"], font=("Segoe UI", 9)).pack(
        fill="x", padx=10, pady=(10, 4))

    listbox = tk.Listbox(win, bg=_P["field"], fg=_P["text"], relief="flat",
                         bd=0, highlightthickness=0,
                         font=("Segoe UI", 10), activestyle="none",
                         selectbackground=_P["combo_bg"])
    listbox.pack(fill="both", expand=True, padx=10, pady=4)

    row = tk.Frame(win, bg=_P["card"])
    row.pack(fill="x", padx=10, pady=4)
    frm_var, to_var = tk.StringVar(), tk.StringVar()
    tk.Entry(row, textvariable=frm_var, bg=_P["field"], fg=_P["text"],
             insertbackground=_P["text"], relief="flat", bd=0, width=16,
             font=("Segoe UI", 10)).pack(side="left", ipady=5)
    tk.Label(row, text="→", bg=_P["card"], fg=_P["hint"]).pack(side="left",
                                                               padx=4)
    tk.Entry(row, textvariable=to_var, bg=_P["field"], fg=_P["text"],
             insertbackground=_P["text"], relief="flat", bd=0, width=16,
             font=("Segoe UI", 10)).pack(side="left", ipady=5)

    def refresh():
        listbox.delete(0, "end")
        for e in load():
            listbox.insert("end", f'{e["from"]}  →  {e["to"]}')

    def do_add():
        if frm_var.get().strip() and to_var.get().strip():
            add(frm_var.get(), to_var.get())
            frm_var.set("")
            to_var.set("")
            refresh()

    def do_del():
        sel = listbox.curselection()
        if sel:
            remove(load()[sel[0]]["from"])
            refresh()

    btns = tk.Frame(win, bg=_P["card"])
    btns.pack(fill="x", padx=10, pady=(4, 10))
    tk.Button(btns, text="Добавить", command=do_add, bg=_P["btn_green"],
              fg="#ffffff", relief="flat", bd=0, activebackground=_P["btn_green"],
              activeforeground="#ffffff", font=("Segoe UI", 9, "bold"),
              padx=12, pady=3, cursor="hand2").pack(side="left")
    tk.Button(btns, text="Удалить выбранное", command=do_del, bg=_P["danger_bg"],
              fg=_P["err"], relief="flat", bd=0, activebackground=_P["danger_bg"],
              activeforeground=_P["err"], font=("Segoe UI", 9),
              padx=12, pady=3, cursor="hand2").pack(side="left", padx=6)

    refresh()
    win.update_idletasks()
    import selector
    vx, vy, vw, vh = selector.virtual_screen()
    win.geometry(f"+{vx + (vw - 520) // 2}+{vy + (vh - 420) // 2}")
    return win
