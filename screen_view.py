"""Полноэкранный оверлей: замороженный снимок с прямоугольниками переводов.

Оверлей показывается сразу после снимка экрана; переводы добавляются
постепенно (пачками) прямо на глазах пользователя. На месте каждого
распознанного блока рисуется непрозрачная плашка с переводом. Если перевод
длиннее оригинала — шрифт уменьшается, пока текст не влезет; в крайнем
случае плашка дорастает вниз. Клик или Esc — закрыть.

Потокобезопасность: пачки передаются через schedule-колбэк приложения
(очередь, опрашиваемая UI-потоком) — root.after из фонового потока
небезопасен. Шрифты кешируются (создание tkfont — дорогой Tcl-вызов).
"""
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageTk

import selector

_BG = "#1b1e23"
_FG = "#ffffff"
_BORDER = "#4CAF50"
_FONT = "Segoe UI"

_FONTS = {}


def _font(size: int) -> tkfont.Font:
    f = _FONTS.get(size)
    if f is None:
        f = _FONTS[size] = tkfont.Font(family=_FONT, size=size)
    return f


def _wrap_lines(font: tkfont.Font, text: str, width: int) -> list:
    """Разбить текст на строки шириной не более width (как это сделает Canvas)."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if not cur or font.measure(trial) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_size(text: str, box_w: int, box_h: int, size_hi: int, size_lo: int = 8) -> int:
    """Максимальный кегль, при котором текст влезает в box_w x box_h."""
    for size in range(int(size_hi), size_lo - 1, -1):
        f = _font(size)
        n = len(_wrap_lines(f, text, box_w))
        if n * f.metrics("linespace") <= box_h:
            return size
    return size_lo


class TranslatedOverlay:
    """Замороженный снимок экрана, на котором постепенно появляются переводы."""

    def __init__(self, parent, screenshot: Image.Image, schedule=None):
        vx, vy, vw, vh = selector.virtual_screen()
        self._vw, self._vh = vw, vh
        self._closed = False
        self._done = 0
        self._total = 0
        # маршалинг в UI-поток: очередь приложения (main.ui_schedule);
        # локальный after — только резерв для автономного использования
        self._schedule = schedule

        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{vw}x{vh}+{vx}+{vy}")

        self.canvas = tk.Canvas(self.win, width=vw, height=vh,
                                highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True)

        photo = ImageTk.PhotoImage(screenshot, master=self.win)
        self.win._photo = photo  # держим ссылку на картинку
        self.canvas.create_image(0, 0, image=photo, anchor="nw")

        # статус в правом верхнем углу
        self.canvas.create_rectangle(vw - 260, 8, vw - 8, 32, fill=_BG, outline="")
        self._status_id = self.canvas.create_text(
            vw - 16, 20, anchor="e", text="Сосна: распознаю текст...",
            fill="#8bc34a", font=(_FONT, 10, "bold"))

        self.canvas.bind("<Button-1>", lambda e: self.close())
        self.win.bind("<Escape>", lambda e: self.close())

        self.win.focus_force()
        self.win.grab_set_global()

    # ---- вызовы из фонового потока ----

    def _to_ui(self, fn):
        if self._schedule is not None:
            self._schedule(fn)
        else:
            try:
                self.win.after(0, fn)
            except RuntimeError:
                pass

    def add_blocks(self, blocks: list):
        """Добавить пачку переведённых блоков (потокобезопасно)."""
        self._to_ui(lambda: self._add_blocks_ui(blocks))

    def finish(self, total: int):
        """Сообщить о завершении (total=0 — текст не найден)."""
        self._to_ui(lambda: self._finish_ui(total))

    def set_total(self, total: int):
        """Общее число блоков (для счётчика прогресса)."""
        self._to_ui(lambda: setattr(self, "_total", total))

    def is_closed(self) -> bool:
        return self._closed

    # ---- только главный поток ----

    def _add_blocks_ui(self, blocks: list):
        if self._closed or not self.win.winfo_exists():
            return
        for b in blocks:
            self._draw_block(b)
            self._done += 1
        self._update_status()

    def _draw_block(self, b: dict):
        x, y = round(b["x"]), round(b["y"])
        w, h = round(b["w"]), round(b["h"])
        tr = (b.get("tr") or "").strip()
        if w < 10 or h < 6 or not tr:
            return
        w = min(w, self._vw - 4)
        x = max(2, min(x, self._vw - w - 4))
        y = max(2, min(y, self._vh - 12))

        # кегль ≈ высота строки оригинала (Tk-кегль в pt ≈ пиксели × 0.75):
        # перевод выглядит как оригинал; уменьшаем только если не влезает
        src_h = b.get("src_h") or h
        size_hi = max(10, min(44, int(src_h * 0.75)))
        size = _fit_size(tr, w - 10, max(h - 4, int(src_h)), size_hi=size_hi)
        f = _font(size)
        n = len(_wrap_lines(f, tr, w - 10))
        text_h = n * f.metrics("linespace") + 6
        box_h = max(h, text_h)
        if y + box_h > self._vh - 4:  # не влезаем вниз — сдвигаем плашку вверх
            box_h = min(box_h, self._vh - 8)
            y = max(2, self._vh - 4 - box_h)

        self.canvas.create_rectangle(
            x - 3, y - 3, x + w + 3, y + box_h,
            fill=_BG, outline=_BORDER, width=1,
        )
        self.canvas.create_text(
            x + 2, y, anchor="nw", width=w - 6, text=tr,
            fill=_FG, font=(_FONT, size), justify="left",
        )

    def _update_status(self):
        if self._total:
            self.canvas.itemconfigure(
                self._status_id, text=f"Сосна: перевожу... {self._done}/{self._total}")

    def _finish_ui(self, total: int):
        if self._closed or not self.win.winfo_exists():
            return
        self._total = total
        if total == 0:
            self.canvas.itemconfigure(self._status_id, text="Сосна: текст не найден")
            self.win.after(2500, self.close)
        else:
            self._update_status()
            self.win.after(1800, lambda: self._safe_forget(self._status_id))

    def _safe_forget(self, item_id):
        if not self._closed and self.win.winfo_exists():
            self.canvas.delete(item_id)

    def close(self):
        self._closed = True
        if self.win.winfo_exists():
            self.win.destroy()


def show_translated(parent, screenshot: Image.Image, schedule=None) -> TranslatedOverlay:
    """Сразу показать замороженный снимок; переводы добавляйте через .add_blocks()."""
    return TranslatedOverlay(parent, screenshot, schedule=schedule)
