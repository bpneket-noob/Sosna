"""Полноэкранный оверлей выделения: «замораживает» экран снимком.

Сразу после хоткея снимаем весь рабочий стол (пока контекстные меню не
исчезли) и показываем снимок в оверлее. Пользователь выделяет фрагмент
на замороженной картинке — реальный экран в это время не участвует.
"""
import ctypes
import tkinter as tk
from PIL import Image, ImageTk

_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


def virtual_screen():
    """Геометрия всего виртуального рабочего стола (все мониторы), физ. пиксели."""
    gsm = ctypes.windll.user32.GetSystemMetrics
    return (
        gsm(_SM_XVIRTUALSCREEN),
        gsm(_SM_YVIRTUALSCREEN),
        gsm(_SM_CXVIRTUALSCREEN),
        gsm(_SM_CYVIRTUALSCREEN),
    )


def grab_screen() -> Image.Image:
    """Мгновенно снять весь виртуальный рабочий стол (без создания окон).

    mss-инстанс переиспользуется в рамках потока — раньше на каждый
    снимок создавался новый (лишняя инициализация GDI)."""
    vx, vy, vw, vh = virtual_screen()
    import ocr
    return ocr.grab_display({"left": vx, "top": vy, "width": vw, "height": vh})


def select_region(parent, screenshot: Image.Image) -> tuple | None:
    """Показать замороженный снимок и вернуть (left, top, width, height) или None.

    ЛКМ-драг — выделение; простой клик или Esc — отмена.
    """
    vx, vy, vw, vh = virtual_screen()

    # лёгкое затемнение копии, чтобы было видно, что экран «заморожен»
    dim = Image.blend(screenshot, Image.new("RGB", screenshot.size, (0, 0, 0)), 0.25)

    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.geometry(f"{vw}x{vh}+{vx}+{vy}")

    canvas = tk.Canvas(win, width=vw, height=vh, highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)

    photo = ImageTk.PhotoImage(dim, master=win)
    win._photo = photo  # держим ссылку, иначе сборщик удалит картинку
    canvas.create_image(0, 0, image=photo, anchor="nw")

    state = {"x0": 0, "y0": 0, "rect": None, "result": None, "pressed": False}

    def on_press(e):
        state["pressed"] = True
        state["x0"], state["y0"] = e.x, e.y
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#4CAF50", width=2
        )

    def on_drag(e):
        if state["rect"] and state["pressed"]:
            canvas.coords(state["rect"], state["x0"], state["y0"], e.x, e.y)

    def on_release(e):
        if state.get("pressed"):
            state["pressed"] = False
            x0, y0 = state["x0"], state["y0"]
            left, right = sorted((x0, e.x))
            top, bottom = sorted((y0, e.y))
            w, h = right - left, bottom - top
            if w >= 3 and h >= 3:
                state["result"] = (vx + left, vy + top, w, h)
        # Release без нажатия на оверлее (кнопка была зажата до его открытия)
        # выделение не создаёт, но оверлей закрываем — это просто отмена
        win.destroy()

    def on_escape(_event):
        win.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    win.bind("<Escape>", on_escape)

    win.focus_force()
    win.grab_set_global()   # клики не уходят в «настоящий» экран под снимком
    parent.wait_window(win)

    return state["result"]
