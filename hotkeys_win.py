"""RegisterHotKey-диспетчер горячих клавиш — основной канал (как у NVIDIA).

Регистрирует комбинации через Win32 RegisterHotKey и получает WM_HOTKEY в
собственный GetMessage-цикл. Доставка гарантируется системой независимо от
загрузки нашего процесса: хук нельзя «снять за медлительность», вход не
тормозится, антивирусы не считают это кейлоггером.

Ограничение RegisterHotKey: комбинация должна быть свободна (первым
пришла — владей), а в эксклюзивном фулскрине WM_HOTKEY иногда теряется.
Поэтому в Сосне это ОСНОВНОЙ канал, а LL-хук-детектор (main._pulse) —
резервный: он видит клавиши раньше всех. Дедупликация пары каналов —
в main (окно 0.5с).

Потокобезопасность: update() можно звать из любого потока — диспетчеру
отправляется сообщение о перезагрузке набора.
"""
import ctypes
from ctypes import wintypes
import threading

import sosna_log

log = sosna_log.get()

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312
WM_APP_RELOAD = 0x8000  # WM_APP
WM_QUIT = 0x0012
INFINITE = 0xFFFFFFFF

_NAME_TO_MOD = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "lctrl": MOD_CONTROL,
    "rctrl": MOD_CONTROL, "left ctrl": MOD_CONTROL, "right ctrl": MOD_CONTROL,
    "alt": MOD_ALT, "lalt": MOD_ALT, "ralt": MOD_ALT,
    "left alt": MOD_ALT, "right alt": MOD_ALT,
    "shift": MOD_SHIFT, "lshift": MOD_SHIFT, "rshift": MOD_SHIFT,
    "left shift": MOD_SHIFT, "right shift": MOD_SHIFT,
    "win": MOD_WIN, "windows": MOD_WIN, "lwin": MOD_WIN, "rwin": MOD_WIN,
    "left windows": MOD_WIN, "right windows": MOD_WIN,
}

_VK_TABLE = {
    "space": 0x20, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "enter": 0x0D,
    "return": 0x0D, "backspace": 0x08, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "page up": 0x21,
    "pgdn": 0x22, "page down": 0x22, "print screen": 0x2C,
    "printscreen": 0x2C, "caps lock": 0x14, "pause": 0x13,
}
for _i in range(1, 25):
    _VK_TABLE[f"f{_i}"] = 0x70 + (_i - 1)
for _i in range(10):
    _VK_TABLE[f"num {_i}"] = 0x60 + _i

_user32 = ctypes.WinDLL("user32", use_last_error=True)


def vk_for(name: str):
    """VK-код клавиши по имени ('t', 'f5', 'num 3', 'insert'...)."""
    name = (name or "").strip().lower()
    if not name:
        return None
    if name in _VK_TABLE:
        return _VK_TABLE[name]
    if len(name) == 1 and "a" <= name <= "z":
        return ord(name.upper())
    if len(name) == 1 and "0" <= name <= "9":
        return ord(name)
    # всё остальное — через скан-код keyboard-библиотеки
    try:
        import keyboard
        codes = keyboard.key_to_scan_codes(name)
        if codes:
            vk = _user32.MapVirtualKeyW(codes[0], 1)  # MAPVK_VSC_TO_VK
            return vk or None
    except Exception:
        pass
    return None


def parse_combo(combo: str):
    """'ctrl+alt+t' → (модификаторы, vk) или None, если разобрать нельзя."""
    parts = [p.strip().lower() for p in (combo or "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        m = _NAME_TO_MOD.get(p)
        if m is None:
            return None
        mods |= m
    vk = vk_for(parts[-1])
    if vk is None:
        return None
    if mods == 0:
        # голая клавиша без модификаторов — глобально перехватывать нельзя:
        # заблокирует набор текста всей системе
        return None
    return mods, vk


class HotkeyDispatcher(threading.Thread):
    """Поток с GetMessage-циклом: владеет регистрациями RegisterHotKey.

    update(combos) — новый набор строк; применение асинхронное (поток
    сам перерегистрирует). on_hotkey(combo_str) зовётся в потоке
    диспетчера при WM_HOTKEY. Ошибки регистрации (комбинация занята
    другим приложением) — через on_failed(combo_str).
    """

    def __init__(self, on_hotkey, on_failed=None):
        super().__init__(name="sosna-hotkey-reg", daemon=True)
        self._on_hotkey = on_hotkey
        self._on_failed = on_failed or (lambda c: None)
        self._pending = None          # список combo_str (под lock)
        self._lock = threading.Lock()
        self._thread_id = 0
        self._registered = {}         # id -> combo_str
        self._failed_prev = set()
        self._stopping = False

    # ---------- публичное (любой поток) ----------

    def update(self, combos):
        """Заменить набор горячих клавиш (список строк)."""
        with self._lock:
            self._pending = list(combos or [])
            tid = self._thread_id
        if not self.is_alive():
            self.start()
            return
        if tid:
            _user32.PostThreadMessageW(tid, WM_APP_RELOAD, 0, 0)

    def stop(self):
        self._stopping = True
        tid = self._thread_id
        if tid:
            _user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)

    # ---------- внутреннее (поток диспетчера) ----------

    def run(self):
        self._thread_id = kernel32_tid()
        log.info("RegisterHotKey-диспетчер запущен (tid=%d)", self._thread_id)
        self._apply_pending()
        msg = wintypes.MSG()
        while not self._stopping:
            r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            if msg.message == WM_HOTKEY:
                combo = self._registered.get(int(msg.wParam))
                if combo:
                    try:
                        self._on_hotkey(combo)
                    except Exception:
                        log.exception("обработчик WM_HOTKEY упал")
            elif msg.message == WM_APP_RELOAD:
                self._apply_pending()
        self._unregister_all()
        log.info("RegisterHotKey-диспетчер остановлен")

    def _apply_pending(self):
        with self._lock:
            combos = self._pending
            self._pending = None
        if combos is None:
            return
        self._unregister_all()
        failed_now = []
        for combo in combos:
            parsed = parse_combo(combo)
            if parsed is None:
                continue  # невалидные/без модификаторов — только LL-канал
            mods, vk = parsed
            # id уникален в рамках потока: vk + модификаторы в старших битах
            hid = (vk & 0xFFFF) | (mods << 16)
            if _user32.RegisterHotKey(None, hid, mods, vk):
                self._registered[hid] = combo
            else:
                err = ctypes.get_last_error()
                failed_now.append(combo)
                log.info("RegisterHotKey «%s» не удался (err=%d) — "
                         "комбинация занята другим приложением: работает "
                         "резервный LL-канал", combo, err)
        newly_failed = [c for c in failed_now if c not in self._failed_prev]
        self._failed_prev = set(failed_now)
        ok = [c for c in combos
              if c in self._registered.values() and c not in failed_now]
        if ok:
            log.info("RegisterHotKey активен: %s", ", ".join(sorted(ok)))
        for c in newly_failed:
            try:
                self._on_failed(c)
            except Exception:
                log.exception("on_failed упал")

    def _unregister_all(self):
        for hid in list(self._registered):
            _user32.UnregisterHotKey(None, hid)
        self._registered.clear()


def kernel32_tid() -> int:
    return ctypes.WinDLL("kernel32").GetCurrentThreadId()
