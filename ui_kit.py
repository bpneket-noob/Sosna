"""UI-кит «Сосны»: гамма флага Республики Коми, градиенты, элевации.

Принципы — Fluent 2 / Material 3 ([дизайн-системы Microsoft/Material]):
глубина через тональную элевацию и мягкие тени, а не чёткие рамки;
контраст текста по WCAG 2.2 — не ниже 4.5:1 для обычного текста.

Цвета флага Коми: синий #0079C2, белый #FFFFFF, зелёный #009543
(полосы 2:1:2). Тёмная тема «Коми» — глубокий зелёный градиент с
синими акцентами, карточки тонами, белый текст (контраст > 10:1).
"""
from PIL import Image, ImageDraw, ImageFilter

try:
    from PIL import ImageTk
except ImportError:  # pragma: no cover
    ImageTk = None

import tkinter as tk

# ---- цвета флага Республики Коми ----
FLAG_BLUE = "#0079C2"
FLAG_WHITE = "#FFFFFF"
FLAG_GREEN = "#009543"

# ---- токены тёмной темы «Коми» (Fluent/M3-подобные роли) ----
P = {
    "grad_top": "#0A2E1A",     # фон-градиент, верх
    "grad_bottom": "#125231",  # фон-градиент, низ (глубже зелёный флага)
    "card": "#16261D",         # поверхность карточки (тональная элевация)
    "card_rim": "#0F1D14",     # тонкая кромка между карточкой и фоном
    "title": "#F2F7F4",
    "text": "#EAF2ED",
    "hint": "#A9C2B2",         # вторичный текст (контраст к card ≥ 4.5:1)
    "field": "#1E3327",        # поля ввода
    "select": "#1E3327",
    "combo_bg": "#22402F",
    "btn": "#22402F",
    "btn_text": "#F2F7F4",
    "accent": "#2FBF71",       # акцент — светлый зелёный флага (на тёмном)
    "btn_green": "#1E7A46",    # фон зелёной кнопки (белый текст ≥ 4.5:1)
    "blue": "#4DA8E8",         # акцент-синий флага (ссылки, фокус)
    "ok": "#2FBF71",
    "err": "#E57373",
    "danger_bg": "#3A1F22",
}

_LIGHT = {
    "grad_top": "#E7F4EC",
    "grad_bottom": "#D8EAF7",
    "card": "#FFFFFF",
    "card_rim": "#E2EBE5",
    "title": "#12281B",
    "text": "#1B2B22",
    "hint": "#5C7265",
    "field": "#F2F7F4",
    "select": "#FFFFFF",
    "combo_bg": "#E4EEE7",
    "btn": "#E4EEE7",
    "btn_text": "#12281B",
    "accent": "#009543",
    "btn_green": "#00713B",   # белый текст ≥ 4.5:1 (флаговый #009543 светл)
    "blue": "#0079C2",
    "ok": "#00713B",
    "err": "#C62828",
    "danger_bg": "#F7E2E2",
}

_theme_cache = {}


def palette(theme: str = "komi") -> dict:
    """Токены темы: komi | dark | light."""
    if theme == "light":
        return dict(_LIGHT)
    if theme == "dark":
        d = dict(_LIGHT)
        d.update({"grad_top": "#1E2124", "grad_bottom": "#171A1C",
                  "card": "#26292E", "card_rim": "#1B1E21",
                  "title": "#D0D6DD", "text": "#E0E0E0", "hint": "#8A919B",
                  "field": "#1B1E23", "select": "#1B1E23",
                  "combo_bg": "#3A4046", "btn": "#3A4046",
                  "btn_text": "#E0E0E0", "accent": "#43A047",
                  "btn_green": "#2E7D32", "blue": "#64B5F6",
                  "ok": "#66BB6A", "err": "#F98B85",
                  "danger_bg": "#5F2120"})
        return d
    return dict(P)  # komi — по умолчанию


# ---------- контраст (WCAG 2.2) ----------

def _srgb(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lum(hexcolor: str) -> float:
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb(r) + 0.7152 * _srgb(g) + 0.0722 * _srgb(b)


def contrast(fg: str, bg: str) -> float:
    """Коэффициент контраста WCAG (4.5 — нормальный текст, 3 — крупный)."""
    l1, l2 = _lum(fg), _lum(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# ---------- градиент и мягкие тени ----------

def _rgb(hexcolor: str):
    h = hexcolor.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def gradient(w: int, h: int, top: str, bottom: str) -> Image.Image:
    """Диагональный градиент top→bottom (быстро: 2x2 + resize)."""
    w, h = max(1, w), max(1, h)
    t, b = _rgb(top), _rgb(bottom)
    g = Image.new("RGB", (2, 2))
    g.putpixel((0, 0), t)
    g.putpixel((1, 0), tuple((a * 3 + c) // 4 for a, c in zip(t, b)))
    g.putpixel((0, 1), tuple((a + c * 3) // 4 for a, c in zip(t, b)))
    g.putpixel((1, 1), b)
    return g.resize((w, h), Image.BILINEAR)


def _stamp_shadow(base: Image.Image, x: int, y: int, w: int, h: int,
                  radius: int = 12, alpha: int = 80, blur: int = 9,
                  grow: int = 16) -> Image.Image:
    """Мягкая тень под прямоугольником карточки (композит по месту)."""
    pad = grow + blur * 2
    bx, by = max(0, x - pad), max(0, y - pad)
    sw, sh = min(base.width - bx, w + 2 * pad), min(base.height - by, h + 2 * pad)
    if sw <= 0 or sh <= 0:
        return base
    mask = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [x - bx, y - by, x - bx + w, y - by + h], radius=radius, fill=alpha)
    mask = mask.filter(ImageFilter.GaussianBlur(blur))
    region = base.crop((bx, by, bx + sw, by + sh))
    black = Image.new("RGB", (sw, sh), (0, 0, 0))
    base.paste(Image.composite(black, region, mask), (bx, by))
    return base


def background_with_shadows(w: int, h: int, card_boxes: list,
                            colors: dict) -> Image.Image:
    """Фон окна: градиент + мягкие тени под карточками (card_boxes =
    [(x, y, w, h)] относительно содержимого). Кэш по сигнатуре."""
    sig = (w, h, colors.get("grad_top"), colors.get("grad_bottom"),
           tuple(card_boxes))
    cached = _theme_cache.get(("bg", sig))
    if cached is not None:
        return cached
    img = gradient(w, h, colors["grad_top"], colors["grad_bottom"])
    for (x, y, bw, bh) in card_boxes:
        if bw > 6 and bh > 6:
            img = _stamp_shadow(img, x, y, bw, bh)
    _theme_cache.clear()          # один последний фон — память не течёт
    _theme_cache[("bg", sig)] = img
    return img


def gradient_photo(canvas, w: int, h: int, boxes: list, colors: dict,
                   tag: str = "bgrad"):
    """Нарисовать градиент с тенями под карточками на canvas (на дне).
    boxes — [(x, y, w, h)] карточек. Возвращает PhotoImage — ссылку
    нужно держать на вызывающей стороне."""
    if ImageTk is None:
        return None
    img = background_with_shadows(w, h, boxes, colors)
    photo = ImageTk.PhotoImage(img, master=canvas)
    canvas.delete(tag)
    item = canvas.create_image(0, 0, anchor="nw", image=photo, tags=(tag,))
    canvas.tag_lower(item)
    return photo


# ---------- круглые кнопки шапок окон ----------

class Tooltip:
    """Всплывающая подсказка при наведении (общая для всех окон)."""

    def __init__(self, widget, get_text, delay=450, bg="#fffbd6", fg="#333333"):
        self.widget = widget
        self.get_text = get_text
        self.delay = delay
        self.bg = bg
        self.fg = fg
        self._tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e):
        if self._after is None:
            self._after = self.widget.after(self.delay, self._show)

    def _show(self):
        text = (self.get_text() or "").strip()
        if not text or self._tip is not None:
            return
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self.widget)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tk.Label(tw, text=text, bg=self.bg, fg=self.fg, relief="solid",
                 bd=1, font=("Segoe UI", 9), justify="left",
                 wraplength=520, padx=6, pady=3).pack()
        tw.geometry(f"+{x}+{y}")

    def _hide(self, _e=None):
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None
        if self._tip is not None and self._tip.winfo_exists():
            self._tip.destroy()
        self._tip = None


def round_button(parent, glyph: str, tooltip: str, command,
                 kind: str = "accent", size: int = 32, font_size: int = 13):
    """Круглая кнопка-канвас для шапок безрамочных окон.

    kind: danger (красная — закрыть), accent (зелёная — действие),
    ghost (тёмная — нейтральное действие). ✕ в шапке упаковывается
    ПЕРВОЙ справа — тогда она оказывается в самом углу.
    """
    p = P
    colors = {"danger": ("#8C2B24", "#C13B31", "#FFD9D4"),
              "accent": ("#1E5B3A", "#2FBF71", "#D9FFE9"),
              "ghost": ("#22402F", "#2F5A42", "#EAF2ED")}
    bg, hover, _fgv = colors.get(kind, colors["accent"])
    fg = "#ffffff" if kind != "ghost" else p["text"]

    c = tk.Canvas(parent, width=size, height=size, highlightthickness=0,
                  bg=parent.cget("bg"), cursor="hand2")
    oval = c.create_oval(1, 1, size - 1, size - 1, fill=bg, outline="")
    txt = c.create_text(size // 2, size // 2, text=glyph, fill=fg,
                        font=("Segoe UI", font_size, "bold"))

    def enter(_e):
        c.itemconfigure(oval, fill=hover)

    def leave(_e):
        c.itemconfigure(oval, fill=bg)

    c.bind("<Enter>", enter)
    c.bind("<Leave>", leave)
    c.bind("<ButtonPress-1>", lambda e: command())
    Tooltip(c, lambda: tooltip)
    return c
