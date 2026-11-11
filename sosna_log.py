"""Логирование «Сосны»: ротация файлов, перехват всех ошибок, память процесса.

Без этого модуля приложение под pythonw.exe немо: print и traceback
уходят в никуда. Теперь каждая ошибка (включая исключения внутри
tkinter-колбэков) попадает в %APPDATA%\\Sosna\\logs\\sosna.log.
"""
import ctypes
import logging
import logging.handlers
import os
import sys
import threading
import time

LOG = logging.getLogger("sosna")
_initialised = False

# класс памяти для GetProcessMemoryInfo
class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def setup(data_dir: str, console: bool = False) -> logging.Logger:
    """Инициализировать логгер (идемпотентно). Возвращает корневой логгер."""
    global _initialised
    if _initialised:
        return LOG
    _initialised = True
    try:
        log_dir = os.path.join(data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "sosna.log"),
            maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
            "%Y-%m-%d %H:%M:%S"))
        LOG.addHandler(handler)
        LOG.setLevel(logging.DEBUG)
        if console and sys.stderr is not None:
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
                "%H:%M:%S"))
            LOG.addHandler(sh)
    except Exception:  # логи не должны ломать приложение ни при каких условиях
        pass
    # сторонние библиотеки слишком разговорчивы в DEBUG
    for noisy in ("urllib3", "requests", "PIL", "winrt",
                  "huggingface_hub", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return LOG


def get() -> logging.Logger:
    return LOG


def install_excepthooks():
    """Перехват необработанных исключений главного потока."""
    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        LOG.critical("Необработанное исключение", exc_info=(exc_type, exc, tb))
        try:
            logging.shutdown()
        except Exception:
            pass
    sys.excepthook = _hook


def install_tk_hook(root):
    """Перехват исключений в колбэках tkinter (кнопки, бинды)."""
    def _cb(self, exc, val, tb):
        LOG.error("Исключение в tkinter-колбэке", exc_info=(exc, val, tb))
    # подменяем на экземпляре — стандартный молча печатает в stderr (None)
    root.report_callback_exception = lambda e, v, t: _cb(root, e, v, t)


def mem_mb() -> int:
    """Рабочий набор процесса в МБ (для контроля утечек)."""
    try:
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        handle = kernel32.GetCurrentProcess()
        # в Win10 функция живёт в kernel32 как K32*, psapi — для старых систем
        getter = getattr(kernel32, "K32GetProcessMemoryInfo", None)
        if getter is None:
            getter = ctypes.windll.psapi.GetProcessMemoryInfo
        getter.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        ok = getter(handle, ctypes.byref(pmc), pmc.cb)
        if ok:
            return int(pmc.WorkingSetSize) // (1024 * 1024)
    except Exception:
        pass
    return -1


class stage:
    """Контекстный менеджер этапа: логирует длительность в DEBUG.

    with sosna_log.stage("ocr", log=LOG):
        ...
    """

    def __init__(self, name, log=None, warn_over=None):
        self.name = name
        self.log = log or LOG
        self.warn_over = warn_over
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        dt = time.time() - self.t0
        if exc_type is not None:
            self.log.error("этап %s упал за %.2fs: %s", self.name, dt, exc)
        elif self.warn_over is not None and dt > self.warn_over:
            self.log.warning("этап %s: %.2fs (медленно)", self.name, dt)
        else:
            self.log.debug("этап %s: %.2fs", self.name, dt)
        return False


def start_mem_logger(interval_sec: int = 60):
    """Периодически писать потребление ОЗУ в лог (контроль утечек)."""
    def loop():
        while True:
            time.sleep(interval_sec)
            LOG.debug("память процесса: %d МБ", mem_mb())
    threading.Thread(target=loop, name="sosna-mem-log", daemon=True).start()
