import ctypes
import glob
import json
import os
import re
import subprocess
import sys
import time
import tkinter as tk

import psutil

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

UPDATE_INTERVAL_MS = 1500
GPU_QUERY_EVERY_N_TICKS = 3  # GPU reads happen roughly every ~4.5s instead of every tick

_APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_APP_DIR, "config.json")
DEFAULT_CONFIG = {
    "font_name": "Consolas" if IS_WINDOWS else ("Menlo" if IS_MACOS else "monospace"),
    "font_size": 11,
    "hide_in_fullscreen": True,
}
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


DEBUG_LOG_PATH = os.path.join(_APP_DIR, "debug.log")
_MAX_DEBUG_LOG_BYTES = 512 * 1024


def log_debug(msg):
    """Best-effort diagnostic log — used to catch rare, hard-to-reproduce
    issues (e.g. the update loop dying silently) without needing a console.
    Truncates itself once it gets too large so it can't grow unbounded."""
    try:
        if os.path.exists(DEBUG_LOG_PATH) and os.path.getsize(DEBUG_LOG_PATH) > _MAX_DEBUG_LOG_BYTES:
            os.remove(DEBUG_LOG_PATH)
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def fmt_pct(pct):
    return f"{pct:.0f}%" if pct is not None else "N/A"


def fmt_speed(bytes_per_sec):
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f}MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.0f}KB/s"
    return f"{bytes_per_sec:.0f}B/s"


def _nvidia_smi_available():
    try:
        kwargs = {"capture_output": True, "text": True, "timeout": 2}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            **kwargs,
        )
        return out.returncode == 0 and out.stdout.strip() != ""
    except Exception:
        return False


def _run_nvidia_smi():
    """Shared across all platforms — nvidia-smi ships for Windows and Linux;
    not applicable on Apple Silicon/AMD Macs but harmless to try."""
    try:
        kwargs = {"capture_output": True, "text": True, "timeout": 2}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            **kwargs,
        )
        values = [float(v) for v in out.stdout.strip().splitlines() if v.strip()]
        return max(values) if values else None
    except Exception:
        return None


# ============================================================================
# Windows backend
# ============================================================================
if IS_WINDOWS:
    from ctypes import wintypes
    import winreg

    SINGLE_INSTANCE_MUTEX_NAME = "CpuMonitorOverlay_SingleInstance_9f3a1c7e"
    ERROR_ALREADY_EXISTS = 183

    _kernel32 = ctypes.windll.kernel32
    _kernel32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
    _MAX_SIZE_T = ctypes.c_size_t(-1).value
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    HWND_TOPMOST = -1
    SWP_NOACTIVATE = 0x0010

    def force_window_position(hwnd, x, y, w, h):
        """Directly enforces position/topmost via Win32 SetWindowPos, as a
        redundant path alongside Tk's own geometry()/attributes("-topmost").
        Guards against a rare observed case where the overlay silently
        stopped tracking a moved taskbar chevron despite reposition()
        running every tick — Tk's geometry manager appeared to get stuck
        while the rest of the app kept running normally. Logs (rate-limited
        by log_debug's own callers) if the Win32 call itself reports failure,
        since silently swallowing that would hide exactly this kind of bug.
        """
        try:
            ok = user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE)
            if not ok:
                err = _kernel32.GetLastError()
                log_debug(f"SetWindowPos FAILED hwnd={hwnd} pos=({x},{y},{w},{h}) err={err}")
            return ok
        except Exception as e:
            log_debug(f"SetWindowPos exception hwnd={hwnd}: {e!r}")
            return False

    def get_window_rect(hwnd):
        r = RECT()
        return r if user32.GetWindowRect(hwnd, ctypes.byref(r)) else None

    pdh = ctypes.windll.pdh
    PDH_FMT_DOUBLE = 0x00000200
    _PID_LUID_RE = re.compile(r"pid_(\d+)_luid_(0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+)")

    def _set_dpi_awareness():
        """Per-Monitor-V2 (via SetThreadDpiAwarenessContext) is required to
        avoid a documented Windows quirk: a caller that's only "Per-Monitor
        aware" (the older v1 API, SetProcessDpiAwareness) can get a STALE,
        internally cached rect back from GetWindowRect/FindWindowEx for
        windows owned by other processes (like the taskbar), and that cache
        doesn't reliably refresh on repeated polling from the same process
        — this was directly observed and is the root cause of the overlay
        appearing to "freeze" at an old taskbar layout while everything else
        kept updating normally. V2 awareness queries live, every time.
        Falls back to the older APIs on Windows versions that lack it
        (pre-1703), which may still show the stale-cache symptom.
        """
        try:
            user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
            if user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
                return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

    _set_dpi_awareness()

    def trim_working_set():
        """Hints Windows to release idle pages back after a memory-heavy burst
        (e.g. enumerating hundreds of GPU performance counters) instead of
        leaving them resident in the process's working set indefinitely."""
        try:
            _kernel32.SetProcessWorkingSetSize(_kernel32.GetCurrentProcess(), _MAX_SIZE_T, _MAX_SIZE_T)
        except Exception:
            pass

    def acquire_single_instance_lock():
        """Returns (handle, is_first_instance). Keep the handle referenced —
        the lock releases when the process exits or the handle is dropped."""
        handle = _kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        already_running = _kernel32.GetLastError() == ERROR_ALREADY_EXISTS
        return handle, not already_running

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG), ("top", wintypes.LONG),
            ("right", wintypes.LONG), ("bottom", wintypes.LONG),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
            ("rcWork", RECT), ("dwFlags", wintypes.DWORD),
        ]

    MONITOR_DEFAULTTONEAREST = 2
    _DESKTOP_SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")

    def get_dock_rect():
        """Rect to dock beside: the "show hidden icons" chevron when present
        and visible (it's the first Button child of TrayNotifyWnd), else the
        overall notification area — which naturally sits flush against the
        visible icons once the chevron is gone, so the overlay tracks either
        case correctly. Returns None if the taskbar can't be found (the
        caller then falls back to a fixed screen corner).
        """
        hwnd_tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd_tray:
            return None
        hwnd_notify = user32.FindWindowExW(hwnd_tray, None, "TrayNotifyWnd", None)
        target = hwnd_notify if hwnd_notify else hwnd_tray

        if hwnd_notify:
            hwnd_chevron = user32.FindWindowExW(hwnd_notify, None, "Button", None)
            if hwnd_chevron and user32.IsWindowVisible(hwnd_chevron):
                chevron_rect = RECT()
                if user32.GetWindowRect(hwnd_chevron, ctypes.byref(chevron_rect)):
                    if chevron_rect.right > chevron_rect.left:
                        target = hwnd_chevron

        rect = RECT()
        if not user32.GetWindowRect(target, ctypes.byref(rect)):
            return None
        return rect

    def is_fullscreen_app_active():
        """True if the foreground window covers its entire monitor — the same
        heuristic Windows' own taskbar auto-hide and other overlay utilities
        use to detect a fullscreen game or video, including
        borderless-fullscreen apps that don't take exclusive D3D fullscreen.
        """
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value in _DESKTOP_SHELL_CLASSES:
            return False
        win_rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(win_rect)):
            return False
        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return False
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
            return False
        mon = mi.rcMonitor
        return (win_rect.left <= mon.left and win_rect.top <= mon.top and
                win_rect.right >= mon.right and win_rect.bottom >= mon.bottom)

    def is_dark_mode():
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            return value == 0
        except Exception:
            return True

    VIRTUAL_ADAPTER_HINTS = (
        "basic render", "basic display", "remote display", "teamviewer",
        "parsec", "virtual", "displaylink",
    )

    class PDH_FMT_COUNTERVALUE(ctypes.Structure):
        _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

    def _short_gpu_label(name, index):
        n = (name or "").upper()
        if any(k in n for k in ("NVIDIA", "GEFORCE", "RTX", "GTX", "QUADRO")):
            return "NV"
        if "INTEL" in n:
            return "iGPU"
        if "AMD" in n or "RADEON" in n:
            return "AMD"
        return f"GPU{index}"

    def read_gpu_engine_per_luid(sample_gap=0.2):
        """Reads per-process GPU Engine utilization via PDH, grouped by
        adapter LUID. Returns (sums, primary_luids): sums maps a LUID string
        to its summed utilization %, and primary_luids is the set of LUIDs
        that had a "System" (PID 4) engine instance — DWM composites via the
        primary/integrated adapter on hybrid-graphics laptops, so this
        reliably identifies it without needing DXGI adapter enumeration.
        """
        hQuery = wintypes.HANDLE()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(hQuery)) != 0:
            return {}, set()
        try:
            path_wild = r"\GPU Engine(*)\Utilization Percentage"
            buf_size = wintypes.DWORD(0)
            pdh.PdhExpandWildCardPathW(None, path_wild, None, ctypes.byref(buf_size), 0)
            if buf_size.value == 0:
                return {}, set()
            buf = ctypes.create_unicode_buffer(buf_size.value)
            if pdh.PdhExpandWildCardPathW(None, path_wild, buf, ctypes.byref(buf_size), 0) != 0:
                return {}, set()

            paths = []
            offset = 0
            while True:
                s = ctypes.wstring_at(ctypes.addressof(buf) + offset * 2)
                if not s:
                    break
                paths.append(s)
                offset += len(s) + 1

            handles = []
            for p in paths:
                h = wintypes.HANDLE()
                if pdh.PdhAddCounterW(hQuery, p, 0, ctypes.byref(h)) == 0:
                    handles.append((p, h))

            pdh.PdhCollectQueryData(hQuery)
            time.sleep(sample_gap)
            pdh.PdhCollectQueryData(hQuery)

            sums = {}
            primary_luids = set()
            for p, h in handles:
                val = PDH_FMT_COUNTERVALUE()
                if pdh.PdhGetFormattedCounterValue(h, PDH_FMT_DOUBLE, None, ctypes.byref(val)) != 0:
                    continue
                m = _PID_LUID_RE.search(p)
                if not m:
                    continue
                pid, luid = int(m.group(1)), m.group(2)
                sums[luid] = sums.get(luid, 0.0) + val.doubleValue
                if pid == 4:
                    primary_luids.add(luid)
            return {k: min(v, 100.0) for k, v in sums.items()}, primary_luids
        except Exception:
            return {}, set()
        finally:
            pdh.PdhCloseQuery(hQuery)

    class GpuReader:
        """NVIDIA usage comes from nvidia-smi (most accurate, no LUID needed
        at all). Any other adapter (e.g. an Intel iGPU) is read via Windows'
        GPUEngine performance counters and identified as the one DWM (PID 4)
        renders through — see read_gpu_engine_per_luid().
        """

        def __init__(self):
            self.has_nvidia_smi = _nvidia_smi_available()
            self.has_pdh = self._pdh_works()
            self.other_label = self._detect_other_label() if self.has_pdh else None

        def _pdh_works(self):
            try:
                read_gpu_engine_per_luid(sample_gap=0.05)
                return True
            except Exception:
                return False

        def _detect_other_label(self):
            try:
                import wmi
                w = wmi.WMI(namespace="root\\CIMV2")
                controllers = [
                    c.Name for c in w.Win32_VideoController()
                    if c.Name and not any(h in c.Name.lower() for h in VIRTUAL_ADAPTER_HINTS)
                ]
                non_nvidia = [c for c in controllers if _short_gpu_label(c, 0) != "NV"]
                return _short_gpu_label(non_nvidia[0], 0) if non_nvidia else None
            except Exception:
                return None

        def read(self):
            """Returns a list of (label, percent) pairs, one per detected GPU."""
            results = []
            if self.has_nvidia_smi:
                nv_val = _run_nvidia_smi()
                if nv_val is not None:
                    results.append(("NV", nv_val))
            if self.has_pdh:
                label = self.other_label or (None if self.has_nvidia_smi else "GPU")
                if label:
                    sums, primary_luids = read_gpu_engine_per_luid()
                    if primary_luids:
                        val = max(sums.get(luid, 0.0) for luid in primary_luids)
                    elif sums and not self.has_nvidia_smi:
                        val = max(sums.values())
                    else:
                        val = 0.0
                    results.append((label, val))
            return results


# ============================================================================
# macOS backend
# ============================================================================
elif IS_MACOS:
    def trim_working_set():
        pass  # no simple equivalent to SetProcessWorkingSetSize on macOS

    def force_window_position(hwnd, x, y, w, h):
        pass  # Tk's own geometry()/attributes("-topmost") is all we have here

    def get_window_rect(hwnd):
        return None

    def acquire_single_instance_lock():
        """Returns (open file handle, is_first_instance). Keep the handle
        referenced — the flock releases when the process exits or the
        handle is closed/garbage-collected."""
        import fcntl
        lock_path = os.path.join(_APP_DIR, ".cpu_monitor.lock")
        f = open(lock_path, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f, True
        except OSError:
            return f, False

    def get_dock_rect():
        return None  # no tray-equivalent API; caller falls back to a fixed corner

    def is_fullscreen_app_active():
        return False  # not implemented — would need pyobjc/Quartz; never auto-hides on macOS

    def is_dark_mode():
        try:
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=1,
            )
            return out.returncode == 0 and "dark" in out.stdout.lower()
        except Exception:
            return True

    class GpuReader:
        """Best-effort: only picks up an NVIDIA GPU via nvidia-smi (rare on
        modern Macs). No public per-GPU utilization API exists for Apple
        Silicon/AMD GPUs without private frameworks, so those show nothing.
        """

        def __init__(self):
            self.has_nvidia_smi = _nvidia_smi_available()

        def read(self):
            if not self.has_nvidia_smi:
                return []
            val = _run_nvidia_smi()
            return [("NV", val)] if val is not None else []


# ============================================================================
# Linux backend
# ============================================================================
else:
    def trim_working_set():
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

    def force_window_position(hwnd, x, y, w, h):
        pass  # Tk's own geometry()/attributes("-topmost") is all we have here

    def get_window_rect(hwnd):
        return None

    def acquire_single_instance_lock():
        """Returns (open file handle, is_first_instance). Keep the handle
        referenced — the flock releases when the process exits or the
        handle is closed/garbage-collected."""
        import fcntl
        lock_path = os.path.join(_APP_DIR, ".cpu_monitor.lock")
        f = open(lock_path, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f, True
        except OSError:
            return f, False

    def get_dock_rect():
        return None  # no single standard tray API across GNOME/KDE/XFCE; caller falls back to a fixed corner

    def is_fullscreen_app_active():
        """Best-effort via xprop (X11 only — no effect under Wayland or if
        xprop isn't installed; never auto-hides in that case)."""
        try:
            active = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                capture_output=True, text=True, timeout=1,
            )
            win_id = active.stdout.strip().split()[-1]
            state = subprocess.run(
                ["xprop", "-id", win_id, "_NET_WM_STATE"],
                capture_output=True, text=True, timeout=1,
            )
            return "_NET_WM_STATE_FULLSCREEN" in state.stdout
        except Exception:
            return False

    def is_dark_mode():
        try:
            out = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True, text=True, timeout=1,
            )
            return "dark" in out.stdout.lower()
        except Exception:
            return True

    class GpuReader:
        """NVIDIA via nvidia-smi; AMD/Intel via the amdgpu driver's sysfs
        gpu_busy_percent file (Intel exposes no equivalent without root +
        intel_gpu_top, so it's not covered here)."""

        def __init__(self):
            self.has_nvidia_smi = _nvidia_smi_available()
            self.amd_paths = sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"))

        def read(self):
            results = []
            if self.has_nvidia_smi:
                val = _run_nvidia_smi()
                if val is not None:
                    results.append(("NV", val))
            for i, path in enumerate(self.amd_paths):
                try:
                    with open(path) as f:
                        val = float(f.read().strip())
                    label = "AMD" if len(self.amd_paths) == 1 else f"AMD{i}"
                    results.append((label, val))
                except Exception:
                    continue
            return results


class TaskbarOverlay:
    GAP = 2

    def __init__(self):
        self.cfg = load_config()
        self.font_name = self.cfg["font_name"]
        self.font_size = self.cfg["font_size"]

        self.gpu_reader = GpuReader()
        self.last_gpu_readings = self.gpu_reader.read()
        self.tick_count = 0
        gpu_labels = [label for label, _ in self.last_gpu_readings] or ["GPU"]
        self.worst_line1 = "CPU:100% " + " ".join(f"{l}:100%" for l in gpu_labels) + " RAM:100%"
        self.worst_line2 = "↑999.9MB/s ↓999.9MB/s"
        psutil.cpu_percent(interval=None)

        net = psutil.net_io_counters()
        self.prev_bytes_sent = net.bytes_sent
        self.prev_bytes_recv = net.bytes_recv
        self.prev_time = time.monotonic()

        dark = is_dark_mode()
        self.bg = "#1f1f1f" if dark else "#f3f3f3"
        self.fg = "#ffffff" if dark else "#1a1a1a"

        # self.root is a hidden Tk() that exists only to own the Tcl
        # interpreter / mainloop and never gets destroyed until Quit.
        # self.win is the actual visible overlay, a Toplevel — if it ever
        # gets stuck (observed: a long-lived window can stop responding to
        # position changes from within its own process, even though a
        # brand-new window always positions correctly), _recreate_window()
        # destroys and rebuilds just the Toplevel, leaving the mainloop and
        # all app state untouched.
        self.root = tk.Tk()
        self.root.withdraw()

        self.is_hidden = False
        self._last_rect_key = "unset"
        self._mismatch_streak = 0
        self.hide_in_fullscreen_var = tk.BooleanVar(value=self.cfg["hide_in_fullscreen"])

        self._build_window()
        self.reposition()
        self.update_values()

    def _build_window(self):
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-toolwindow", True)
        except Exception:
            pass
        self.win.configure(bg=self.bg)

        self.label = tk.Label(
            self.win, text="", bg=self.bg, fg=self.fg, padx=8, justify="left",
        )
        self.label.pack(fill="both", expand=True)
        self.apply_font()

        menu = tk.Menu(self.win, tearoff=0)
        menu.add_command(label="Font size +", command=lambda: self.change_font_size(1))
        menu.add_command(label="Font size -", command=lambda: self.change_font_size(-1))
        menu.add_separator()
        menu.add_checkbutton(
            label="Hide with taskbar (fullscreen apps)",
            variable=self.hide_in_fullscreen_var,
            command=self.toggle_hide_in_fullscreen,
        )
        menu.add_separator()
        menu.add_command(label="Quit monitor", command=self.root.destroy)

        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)

        for button in ("<Button-3>", "<Button-2>"):
            self.label.bind(button, show_menu)
            self.win.bind(button, show_menu)

    def _recreate_window(self):
        log_debug("recreating overlay window to recover from a stuck position")
        was_hidden = self.is_hidden
        try:
            self.win.destroy()
        except Exception:
            pass
        self._build_window()
        if was_hidden:
            self.win.withdraw()
        self._mismatch_streak = 0

    def apply_font(self):
        self.label.config(font=(self.font_name, self.font_size))
        max_chars = max(len(self.worst_line1), len(self.worst_line2))
        self.width = int(max_chars * self.font_size * 0.6) + 24
        self.height_needed = int(self.font_size * 3.2) + 14

    def change_font_size(self, delta):
        self.font_size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, self.font_size + delta))
        self.cfg["font_size"] = self.font_size
        save_config(self.cfg)
        self.apply_font()
        self.reposition()

    def toggle_hide_in_fullscreen(self):
        self.cfg["hide_in_fullscreen"] = self.hide_in_fullscreen_var.get()
        save_config(self.cfg)
        if not self.cfg["hide_in_fullscreen"] and self.is_hidden:
            self.win.deiconify()
            self.is_hidden = False

    def reposition(self):
        rect = get_dock_rect()
        rect_key = (rect.left, rect.top, rect.right, rect.bottom) if rect else None
        if rect_key != self._last_rect_key:
            log_debug(f"dock rect changed: {self._last_rect_key} -> {rect_key}")
            self._last_rect_key = rect_key

        if rect:
            height = max(rect.bottom - rect.top, self.height_needed)
            x = rect.left - self.width - self.GAP
            y = rect.top
        else:
            height = self.height_needed
            screen_w = self.win.winfo_screenwidth()
            screen_h = self.win.winfo_screenheight()
            x = screen_w - self.width - self.GAP
            y = self.GAP if IS_MACOS else screen_h - height - self.GAP
        self.win.geometry(f"{self.width}x{height}+{x}+{y}")
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.update_idletasks()
        force_window_position(self.win.winfo_id(), x, y, self.width, height)

        actual = get_window_rect(self.win.winfo_id())
        if actual and (actual.left != x or actual.top != y):
            self._mismatch_streak += 1
            log_debug(
                f"position mismatch (streak={self._mismatch_streak}): "
                f"intended=({x},{y}) actual=({actual.left},{actual.top}) "
                f"hwnd={self.win.winfo_id()}"
            )
            if self._mismatch_streak >= 2:
                self._recreate_window()
                self.reposition()
        else:
            self._mismatch_streak = 0

    def update_values(self):
        """Thin wrapper that GUARANTEES the periodic tick keeps firing even
        if something inside _tick() raises — without this, a single
        uncaught exception would silently kill the reschedule and freeze
        the overlay (text and position both) until manually restarted."""
        try:
            self._tick()
        except Exception as e:
            log_debug(f"update_values error: {e!r}")
        finally:
            self.root.after(UPDATE_INTERVAL_MS, self.update_values)

    def _tick(self):
        should_hide = self.cfg["hide_in_fullscreen"] and is_fullscreen_app_active()
        if should_hide != self.is_hidden:
            if should_hide:
                self.win.withdraw()
            else:
                self.win.deiconify()
            self.is_hidden = should_hide

        if should_hide:
            return

        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent

        due_for_gpu_query = self.tick_count % GPU_QUERY_EVERY_N_TICKS == 0
        self.tick_count += 1
        if due_for_gpu_query:
            self.last_gpu_readings = self.gpu_reader.read()
            trim_working_set()
        gpu_readings = self.last_gpu_readings
        gpu_part = (
            " ".join(f"{label}:{fmt_pct(val)}" for label, val in gpu_readings)
            if gpu_readings else "GPU:N/A"
        )

        now = time.monotonic()
        net = psutil.net_io_counters()
        elapsed = max(now - self.prev_time, 0.001)
        up_speed = (net.bytes_sent - self.prev_bytes_sent) / elapsed
        down_speed = (net.bytes_recv - self.prev_bytes_recv) / elapsed
        self.prev_bytes_sent = net.bytes_sent
        self.prev_bytes_recv = net.bytes_recv
        self.prev_time = now

        line1 = f"CPU:{fmt_pct(cpu)} {gpu_part} RAM:{fmt_pct(ram)}"
        line2 = f"↑{fmt_speed(up_speed)} ↓{fmt_speed(down_speed)}"
        self.label.config(text=f"{line1}\n{line2}")
        self.reposition()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    _lock_handle, _is_first_instance = acquire_single_instance_lock()
    if not _is_first_instance:
        sys.exit(0)
    TaskbarOverlay().run()
