"""История переводов «Сосны»: хранение и окно просмотра.

Файл — в %APPDATA% (переживает переустановку), запись атомарная с .bak:
сбой в момент записи больше не уничтожает историю молча. Метки времени
с годом. Копирование берёт данные записи напрямую, а не парсит текст
метки (раньше многострочные переводы копировались обрезанными).
"""
import os
import threading
import time
import tkinter as tk

import config
import selector
import ui_kit

_P = ui_kit.palette("komi")

_PATH = os.path.join(config.DATA_DIR, "history.json")
_LIMIT = 200
_lock = threading.Lock()


def record(original: str, translated: str, direction: str = ""):
    """Добавить запись в историю (безопасно из любого потока)."""
    if not original or not translated:
        return
    entry = {
        "ts": time.strftime("%d.%m.%Y %H:%M"),
        "dir": direction,
        "src": original[:2000],
        "dst": translated[:4000],
    }
    with _lock:
        data = _load()
        data.insert(0, entry)
        config.save_json_atomic(_PATH, data[:_LIMIT], indent=1)


def _load() -> list:
    data, _src = config.load_json_with_bak(_PATH)
    if isinstance(data, list):
        return data
    return []


def clear():
    with _lock:
        if os.path.exists(_PATH):
            os.remove(_PATH)
        for suffix in (".bak", ".tmp"):
            p = _PATH + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def _copy(widget, text, btn):
    widget.clipboard_clear()
    widget.clipboard_append(text)
    old = btn.cget("text")
    btn.configure(text="Скопировано ✓")
    btn.after(1200, lambda: btn.configure(text=old))


def open_history(parent):
    """Окно истории: поиск, просмотр, копирование, очистка."""
    win = tk.Toplevel(parent)
    win.title("Сосна — история переводов")
    win.geometry("780x520")
    win.configure(bg=_P["card"])

    top = tk.Frame(win, bg=_P["card"])
    top.pack(fill="x", padx=10, pady=(10, 4))
    search_var = tk.StringVar()
    tk.Entry(top, textvariable=search_var, bg=_P["field"], fg=_P["text"],
             insertbackground=_P["text"], relief="flat", bd=0,
             font=("Segoe UI", 10)).pack(side="left", fill="x", expand=True,
                                         ipady=5, padx=(0, 8))

    mid = tk.Frame(win, bg=_P["card"])
    mid.pack(fill="both", expand=True, padx=10, pady=4)

    listbox = tk.Listbox(mid, bg=_P["field"], fg=_P["text"], relief="flat",
                         bd=0, highlightthickness=0,
                         font=("Segoe UI", 10), activestyle="none",
                         selectbackground=_P["combo_bg"])
    listbox.pack(side="left", fill="both", expand=True)
    scroll = tk.Scrollbar(mid, command=listbox.yview)
    scroll.pack(side="right", fill="y")
    listbox.configure(yscrollcommand=scroll.set)

    detail_var = tk.StringVar(value="")
    detail = tk.Label(win, textvariable=detail_var, bg=_P["btn"], fg=_P["hint"],
                      font=("Segoe UI", 9), wraplength=740, justify="left",
                      anchor="nw", relief="flat", padx=8, pady=6)
    detail.pack(fill="x", padx=10, pady=4)

    bottom = tk.Frame(win, bg=_P["card"])
    bottom.pack(fill="x", padx=10, pady=(4, 10))

    with _lock:
        state = {"data": _load(), "visible": []}

    def refresh():
        q = search_var.get().strip().lower()
        listbox.delete(0, "end")
        state["visible"] = []
        for e in state["data"]:
            line = f'[{e["ts"]}] {e["src"][:60]}  →  {e["dst"][:70]}'
            if not q or q in e["src"].lower() or q in e["dst"].lower():
                listbox.insert("end", line)
                state["visible"].append(e)
        if not state["data"]:
            listbox.insert("end", "История пока пуста")

    def on_select(_e=None):
        sel = listbox.curselection()
        if not sel or sel[0] >= len(state["visible"]):
            return
        e = state["visible"][sel[0]]
        detail_var.set(f'{e["ts"]} {e.get("dir", "")}\n▣ {e["src"]}\n⮞ {e["dst"]}')

    def _current():
        sel = listbox.curselection()
        if not sel or sel[0] >= len(state["visible"]):
            return None
        return state["visible"][sel[0]]

    def do_copy_src():
        e = _current()
        if e:
            _copy(win, e["src"], btn_src)

    def do_copy_dst():
        e = _current()
        if e:
            _copy(win, e["dst"], btn_dst)

    def do_clear():
        clear()
        state["data"] = []
        state["visible"] = []
        detail_var.set("")
        refresh()

    search_var.trace_add("write", lambda *_: refresh())
    listbox.bind("<<ListboxSelect>>", on_select)
    listbox.bind("<Double-Button-1>", lambda e: do_copy_dst())

    btn_src = tk.Button(bottom, text="Копировать оригинал", command=do_copy_src,
                        bg=_P["btn"], fg=_P["btn_text"], relief="flat", bd=0,
                        activebackground=_P["combo_bg"],
                        activeforeground=_P["btn_text"],
                        font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2")
    btn_src.pack(side="left")
    btn_dst = tk.Button(bottom, text="📋 Копировать перевод", command=do_copy_dst,
                        bg=_P["btn_green"], fg="#ffffff", relief="flat", bd=0,
                        activebackground=_P["btn_green"],
                        activeforeground="#ffffff",
                        font=("Segoe UI", 9, "bold"), padx=10, pady=3,
                        cursor="hand2")
    btn_dst.pack(side="left", padx=6)
    tk.Button(bottom, text="Очистить историю", command=do_clear,
              bg=_P["danger_bg"], fg=_P["err"], relief="flat", bd=0,
              activebackground=_P["danger_bg"], activeforeground=_P["err"],
              font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2").pack(side="right")

    refresh()
    vx, vy, vw, vh = selector.virtual_screen()
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    win.geometry(f"+{vx + max(0, (vw - w) // 2)}+{vy + max(0, (vh - h) // 2)}")
    return win
