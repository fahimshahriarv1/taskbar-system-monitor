import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import time
import tkinter as tk
import winreg

import psutil

UPDATE_INTERVAL_MS = 1500

SINGLE_INSTANCE_MUTEX_NAME = "CpuMonitorOverlay_SingleInstance_9f3a1c7e"
ERROR_ALREADY_EXISTS = 183


def acquire_single_instance_lock():
    """Returns False (and leaves a stale handle unclaimed) if another copy is already running."""
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    already_running = ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    return handle, not already_running

_APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_APP_DIR, "config.json")
DEFAULT_CONFIG = {"font_name": "Consolas", "font_size": 11}
MIN_FONT_SIZE = 7
MAX_FONT_SIZE = 24


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            cfg.update({k: data[k] for k in DEFAULT_CONFIG if k in data})
        except Exception:
            pass
    else:
        save_config(cfg)
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def get_tray_rect():
    hwnd_tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd_tray:
        return None
    hwnd_notify = user32.FindWindowExW(hwnd_tray, None, "TrayNotifyWnd", None)
    rect = RECT()
    target = hwnd_notify if hwnd_notify else hwnd_tray
    if not user32.GetWindowRect(target, ctypes.byref(rect)):
        return None
    return rect


def is_dark_taskbar():
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return value == 0
    except Exception:
        return True


class GpuReader:
    """Auto-detects an available way to read GPU utilization and reads it."""

    def __init__(self):
        self.mode = None
        self._wmi = None
        self._detect()

    def _nvidia_smi_works(self):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return out.returncode == 0 and out.stdout.strip() != ""
        except Exception:
            return False

    def _wmi_engine_works(self):
        try:
            import wmi
            self._wmi = wmi.WMI(namespace="root\\CIMV2")
            self._wmi.Win32_PerfFormattedData_Counters_GPUEngine()
            return True
        except Exception:
            return False

    def _detect(self):
        if self._nvidia_smi_works():
            self.mode = "nvidia_smi"
        elif self._wmi_engine_works():
            self.mode = "wmi_engine"
        else:
            self.mode = None

    def read(self):
        if self.mode == "nvidia_smi":
            return self._read_nvidia_smi()
        if self.mode == "wmi_engine":
            return self._read_wmi_engine()
        return None

    def _read_nvidia_smi(self):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            values = [float(v) for v in out.stdout.strip().splitlines() if v.strip()]
            if not values:
                return None
            return max(values)
        except Exception:
            return None

    def _read_wmi_engine(self):
        try:
            engines = self._wmi.Win32_PerfFormattedData_Counters_GPUEngine()
            total = sum(int(e.UtilizationPercentage) for e in engines)
            return min(total, 100.0)
        except Exception:
            return None


def fmt_pct(pct):
    return f"{pct:.0f}%" if pct is not None else "N/A"


def fmt_speed(bytes_per_sec):
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f}MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.0f}KB/s"
    return f"{bytes_per_sec:.0f}B/s"


class TaskbarOverlay:
    GAP = 4

    def __init__(self):
        self.cfg = load_config()
        self.font_name = self.cfg["font_name"]
        self.font_size = self.cfg["font_size"]

        self.gpu_reader = GpuReader()
        psutil.cpu_percent(interval=None)

        net = psutil.net_io_counters()
        self.prev_bytes_sent = net.bytes_sent
        self.prev_bytes_recv = net.bytes_recv
        self.prev_time = time.monotonic()

        dark = is_dark_taskbar()
        self.bg = "#1f1f1f" if dark else "#f3f3f3"
        self.fg = "#ffffff" if dark else "#1a1a1a"

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.root.configure(bg=self.bg)

        self.label = tk.Label(
            self.root, text="", bg=self.bg, fg=self.fg, padx=8, justify="left",
        )
        self.label.pack(fill="both", expand=True)
        self.apply_font()

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Font size +", command=lambda: self.change_font_size(1))
        menu.add_command(label="Font size -", command=lambda: self.change_font_size(-1))
        menu.add_separator()
        menu.add_command(label="Quit monitor", command=self.root.destroy)

        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)

        self.label.bind("<Button-3>", show_menu)
        self.root.bind("<Button-3>", show_menu)

        self.reposition()
        self.update_values()

    def apply_font(self):
        self.label.config(font=(self.font_name, self.font_size))
        self.width = int(self.font_size * 15) + 40
        self.height_needed = int(self.font_size * 3.2) + 14

    def change_font_size(self, delta):
        self.font_size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, self.font_size + delta))
        self.cfg["font_size"] = self.font_size
        save_config(self.cfg)
        self.apply_font()
        self.reposition()

    def reposition(self):
        rect = get_tray_rect()
        if rect:
            height = max(rect.bottom - rect.top, self.height_needed)
            x = rect.left - self.width - self.GAP
            y = rect.top
        else:
            height = self.height_needed
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            x = screen_w - self.width - 200
            y = screen_h - height
        self.root.geometry(f"{self.width}x{height}+{x}+{y}")
        self.root.lift()
        self.root.attributes("-topmost", True)

    def update_values(self):
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        gpu = self.gpu_reader.read()

        now = time.monotonic()
        net = psutil.net_io_counters()
        elapsed = max(now - self.prev_time, 0.001)
        up_speed = (net.bytes_sent - self.prev_bytes_sent) / elapsed
        down_speed = (net.bytes_recv - self.prev_bytes_recv) / elapsed
        self.prev_bytes_sent = net.bytes_sent
        self.prev_bytes_recv = net.bytes_recv
        self.prev_time = now

        line1 = f"CPU:{fmt_pct(cpu)} GPU:{fmt_pct(gpu)} RAM:{fmt_pct(ram)}"
        line2 = f"↑{fmt_speed(up_speed)} ↓{fmt_speed(down_speed)}"
        self.label.config(text=f"{line1}\n{line2}")
        self.reposition()

        self.root.after(UPDATE_INTERVAL_MS, self.update_values)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    _mutex_handle, _is_first_instance = acquire_single_instance_lock()
    if not _is_first_instance:
        sys.exit(0)
    TaskbarOverlay().run()
