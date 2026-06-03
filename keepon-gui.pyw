#!/usr/bin/env python3
# File: keepon-gui.pyw
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-05-30
# Description: System tray app — prevent sleep, bring windows to front on idle
#              (state-preserving), run idle commands, hot-reload config.
# License: MIT

import sys
import os
import ctypes
import ctypes.wintypes
import time
import platform
import subprocess
import configparser
import logging
import traceback
from pathlib import Path
from threading import Thread, Event, Lock
from typing import List, Optional

from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction  # type: ignore
from PyQt5.QtGui import QIcon  # type: ignore
from PyQt5.QtCore import QFileSystemWatcher, QTimer, pyqtSignal, QObject  # type: ignore

try:
    from gntplib import Publisher  # type: ignore
    _growl_icon = Path(__file__).parent / "icons" / "heart.png"
    growl = Publisher(  # type: ignore
        "KeepOn",
        ["info", "error", "warning", "debug", "reload", "run"],
        icon=str(_growl_icon),
    )
    try:
        growl.register()
    except Exception:
        pass
    HAS_GNTPLIB = True
except Exception as e:
    print(f"[GNTPLIB:WARN] gntplib unavailable — Growl notifications disabled: {e}")
    HAS_GNTPLIB = False


def _growl(level: str, title: str, message: str) -> None:
    """Central growl helper — no-ops cleanly when gntplib is absent."""
    if HAS_GNTPLIB:
        try:
            growl.publish(level, title, message)
        except Exception as exc:
            log.debug("growl.publish failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Logging — set up early; level/file overridden after config load
# ─────────────────────────────────────────────────────────────────────────────
_LOG_FMT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.DEBUG,
    format=_LOG_FMT,
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("keepon")

# ─────────────────────────────────────────────────────────────────────────────
# Windows API — constants
# ─────────────────────────────────────────────────────────────────────────────
ES_CONTINUOUS       = 0x80000000
ES_SYSTEM_REQUIRED  = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

SW_SHOWNOACTIVATE   = 4   # show (any state → visible) without stealing focus
SW_MAXIMIZE         = 3

HWND_TOP            = 0

SWP_NOSIZE          = 0x0001
SWP_NOMOVE          = 0x0002
SWP_NOACTIVATE      = 0x0010
SWP_NOOWNERZORDER   = 0x0200
SWP_SHOWWINDOW      = 0x0040

# ─────────────────────────────────────────────────────────────────────────────
# Windows API — structs
# ─────────────────────────────────────────────────────────────────────────────
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left",   ctypes.c_int32), ("top",    ctypes.c_int32),
        ("right",  ctypes.c_int32), ("bottom", ctypes.c_int32),
    ]


class WINDOWPLACEMENT(ctypes.Structure):
    """44 bytes on all Windows targets."""
    _fields_ = [
        ("length",           ctypes.c_uint32),
        ("flags",            ctypes.c_uint32),
        ("showCmd",          ctypes.c_uint32),
        ("ptMinPosition",    _POINT),
        ("ptMaxPosition",    _POINT),
        ("rcNormalPosition", _RECT),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


# ─────────────────────────────────────────────────────────────────────────────
# Windows API — function references with explicit argtypes / restype
# ─────────────────────────────────────────────────────────────────────────────
_u32 = ctypes.windll.user32
_k32 = ctypes.windll.kernel32

_u32.GetWindowPlacement.argtypes   = [ctypes.wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
_u32.GetWindowPlacement.restype    = ctypes.wintypes.BOOL
_u32.SetWindowPlacement.argtypes   = [ctypes.wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
_u32.SetWindowPlacement.restype    = ctypes.wintypes.BOOL
_u32.SetWindowPos.argtypes         = [
    ctypes.wintypes.HWND, ctypes.wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_uint,
]
_u32.SetWindowPos.restype          = ctypes.wintypes.BOOL
_u32.ShowWindow.argtypes           = [ctypes.wintypes.HWND, ctypes.c_int]
_u32.ShowWindow.restype            = ctypes.wintypes.BOOL
_u32.BringWindowToTop.argtypes     = [ctypes.wintypes.HWND]
_u32.BringWindowToTop.restype      = ctypes.wintypes.BOOL
_u32.SetForegroundWindow.argtypes  = [ctypes.wintypes.HWND]
_u32.SetForegroundWindow.restype   = ctypes.wintypes.BOOL
_u32.GetForegroundWindow.argtypes  = []
_u32.GetForegroundWindow.restype   = ctypes.wintypes.HWND
_u32.GetWindowThreadProcessId.argtypes = [
    ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)
]
_u32.GetWindowThreadProcessId.restype  = ctypes.wintypes.DWORD
_u32.AttachThreadInput.argtypes    = [
    ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.BOOL
]
_u32.AttachThreadInput.restype     = ctypes.wintypes.BOOL
_u32.IsWindowVisible.argtypes      = [ctypes.wintypes.HWND]
_u32.IsWindowVisible.restype       = ctypes.wintypes.BOOL
_u32.IsIconic.argtypes             = [ctypes.wintypes.HWND]
_u32.IsIconic.restype              = ctypes.wintypes.BOOL
_u32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
_u32.GetWindowTextLengthW.restype  = ctypes.c_int
_u32.GetWindowTextW.argtypes       = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
_u32.GetWindowTextW.restype        = ctypes.c_int
_u32.EnumWindows.argtypes          = [
    ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.py_object),
    ctypes.py_object,
]
_u32.EnumWindows.restype           = ctypes.wintypes.BOOL
_u32.GetLastInputInfo.argtypes     = [ctypes.POINTER(LASTINPUTINFO)]
_u32.GetLastInputInfo.restype      = ctypes.wintypes.BOOL
_k32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
_k32.SetThreadExecutionState.restype  = ctypes.c_uint32
_k32.GetTickCount.argtypes         = []
_k32.GetTickCount.restype          = ctypes.c_uint32
_k32.GetLastError.argtypes         = []
_k32.GetLastError.restype          = ctypes.c_uint32
_k32.GetCurrentThreadId.argtypes   = []
_k32.GetCurrentThreadId.restype    = ctypes.wintypes.DWORD

# ─────────────────────────────────────────────────────────────────────────────
# Sleep prevention
# ─────────────────────────────────────────────────────────────────────────────
def prevent_sleep() -> None:
    _k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)


def allow_sleep() -> None:
    _k32.SetThreadExecutionState(ES_CONTINUOUS)


# ─────────────────────────────────────────────────────────────────────────────
# Idle time
# ─────────────────────────────────────────────────────────────────────────────
def get_idle_seconds() -> float:
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if _u32.GetLastInputInfo(ctypes.byref(lii)):
        # c_int32 cast handles the 32-bit tick-count wraparound correctly
        elapsed_ms = ctypes.c_int32(_k32.GetTickCount() - lii.dwTime).value
        return max(0, elapsed_ms) / 1000.0
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Window enumeration
# ─────────────────────────────────────────────────────────────────────────────
def _enum_cb(hwnd, results):
    if _u32.IsWindowVisible(hwnd):
        length = _u32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            _u32.GetWindowTextW(hwnd, buf, length + 1)
            results.append((hwnd, buf.value))
    return True


_ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.py_object)


def find_windows_by_title(pattern: str) -> List[int]:
    """All visible HWNDs whose title contains *pattern* (case-insensitive)."""
    results: List[tuple] = []
    _u32.EnumWindows(_ENUM_PROC(_enum_cb), ctypes.py_object(results))
    pl = pattern.lower()
    return [h for h, t in results if pl in t.lower()]


def bring_to_front_preserve_state(hwnd: int) -> bool:
    try:
        # ── 1. Snapshot the current placement ────────────────────────────────
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not _u32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            log.warning("GetWindowPlacement failed hwnd=%s err=%s",
                        hwnd, _k32.GetLastError())
            return False
        original_show_cmd = wp.showCmd
        log.debug("hwnd=%s showCmd=%s before raise", hwnd, original_show_cmd)

        # ── 2. Attach input queues so foreground lock is bypassed ─────────────
        fg_hwnd   = _u32.GetForegroundWindow()
        fg_tid    = _u32.GetWindowThreadProcessId(fg_hwnd, None)
        target_tid = _u32.GetWindowThreadProcessId(hwnd, None)
        our_tid   = _k32.GetCurrentThreadId()

        attached_fg   = False
        attached_target = False

        if fg_tid and fg_tid != our_tid:
            attached_fg = bool(_u32.AttachThreadInput(our_tid, fg_tid, True))
        if target_tid and target_tid != our_tid and target_tid != fg_tid:
            attached_target = bool(_u32.AttachThreadInput(our_tid, target_tid, True))

        try:
            # ── 3. Handle visibility without activating ───────────────────────
            if _u32.IsIconic(hwnd):
                # If minimized, show it without giving it focus
                _u32.ShowWindow(hwnd, 4)  # 4 = SW_SHOWNOACTIVATE
            else:
                # If already visible (Normal or Maximized), just ensure it's visible without focus
                _u32.ShowWindow(hwnd, 8)  # 8 = SW_SHOWNA

            # ── 4. Raise Z-order safely ──────────────────────────────────────
            # This shifts the window to the top layout layer without moving,
            # resizing, or activating it. Your typing focus stays on the old window.
            _u32.SetWindowPos(
                hwnd, HWND_TOP,
                0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE |
                SWP_NOOWNERZORDER | SWP_SHOWWINDOW,
            )

        finally:
            # ── 5. Always detach input threads ───────────────────────────────
            if attached_fg:
                _u32.AttachThreadInput(our_tid, fg_tid, False)
            if attached_target:
                _u32.AttachThreadInput(our_tid, target_tid, False)

        # ── 6. State-Preservation Condition ──────────────────────────────────
        # FIX: If the window was already MAXIMIZED, calling SetWindowPlacement
        # breaks the layout. We ONLY restore placement if it wasn't maximized.
        if original_show_cmd != SW_MAXIMIZE:
            _u32.SetWindowPlacement(hwnd, ctypes.byref(wp))
        else:
            log.debug("hwnd=%s is maximized; skipping SetWindowPlacement to prevent unwanted resizing", hwnd)

        log.debug("hwnd=%s successfully processed without focus theft or structural changes", hwnd)
        return True

    except Exception:
        log.error("bring_to_front_preserve_state error:\n%s", traceback.format_exc())
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Resource path helper
# ─────────────────────────────────────────────────────────────────────────────
def resource_path(relative_path: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)  # type: ignore
    return os.path.join(os.path.abspath("."), relative_path)


# ─────────────────────────────────────────────────────────────────────────────
# Config — candidate locations
# ─────────────────────────────────────────────────────────────────────────────
CONFIG_FILENAME = "keepon.ini"


def _config_candidates() -> List[Path]:
    candidates: List[Path] = []
    plat = platform.system()

    if plat == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.append(Path(appdata) / "keepon" / CONFIG_FILENAME)
    elif plat == "Darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support" / "keepon" / CONFIG_FILENAME
        )
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        candidates.append(Path(xdg) / "keepon" / CONFIG_FILENAME)

    candidates.append(Path.home() / f".{CONFIG_FILENAME}")
    candidates.append(Path(sys.executable).parent / CONFIG_FILENAME)
    candidates.append(Path.cwd() / CONFIG_FILENAME)
    return candidates


def find_config_path() -> Optional[Path]:
    """Return the first existing candidate path, or None."""
    for p in _config_candidates():
        if p.exists():
            return p
    return None


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.optionxform = str

    path = find_config_path()
    if path:
        cfg.read(str(path), encoding="utf-8")
        log.info("Loaded config from: %s", path)
    else:
        default_path = Path.cwd() / CONFIG_FILENAME
        _write_default_config(default_path)
        cfg.read(str(default_path), encoding="utf-8")
        log.info("Created default config at: %s", default_path)

    return cfg


def _write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """\
[keepon]
prevent_sleep      = true
heartbeat_interval = 30

[idle]
idle_threshold          = 60
bring_to_front_windows  =

[idle_commands]
# example = echo idle >> C:\\keepon_idle.log

[logging]
level = INFO
file  =
""",
        encoding="utf-8",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sleep preventer thread
#
# Fix: interval is now read inside the loop under _lock so a hot-reload
# from the main thread is race-free.  A dedicated _wake Event lets
# reload_interval() interrupt the current sleep immediately.
# ─────────────────────────────────────────────────────────────────────────────
class SleepPreventer(Thread):
    def __init__(self, interval: float = 30):
        super().__init__(daemon=True, name="SleepPreventer")
        self._stop   = Event()
        self._wake   = Event()   # set by reload_interval() to abort current sleep early
        self._active = Event()
        self._active.set()
        self._lock    = Lock()
        self._interval = interval

    # ── public API ────────────────────────────────────────────────────────────
    @property
    def is_active(self) -> bool:
        return self._active.is_set()

    def enable(self) -> None:
        self._active.set()

    def disable(self) -> None:
        self._active.clear()
        allow_sleep()

    def reload_interval(self, new_interval: float) -> None:
        """Thread-safe interval update; interrupts the current sleep immediately."""
        with self._lock:
            self._interval = new_interval
        self._wake.set()   # wake the sleeping thread so it picks up the new value

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()   # unblock any ongoing wait

    # ── internal ──────────────────────────────────────────────────────────────
    def run(self) -> None:
        log.debug("SleepPreventer started")
        while not self._stop.is_set():
            if self._active.is_set():
                prevent_sleep()
                log.debug("heartbeat: sleep prevented")

            with self._lock:
                interval = self._interval

            # Wait for the interval OR an early wake (reload / stop)
            self._wake.wait(timeout=interval)
            self._wake.clear()

        allow_sleep()
        log.debug("SleepPreventer stopped")


# ─────────────────────────────────────────────────────────────────────────────
# Idle monitor thread
#
# Key fixes vs original:
#   1. Single `_state` string ("active" | "idle_fired") replaces the
#      was_idle/fired dual-boolean.  A brief mouse wiggle resets to "active"
#      exactly once; there is no way to fire twice within one idle session.
#   2. All config fields are snapshotted in ONE lock acquisition at the top
#      of each poll loop, preventing a hot-reload from racing with a firing
#      idle action.
#   3. growl calls are routed through _growl() which no-ops when gntplib
#      is absent — NameError can no longer silence idle actions.
# ─────────────────────────────────────────────────────────────────────────────
class IdleMonitor(Thread):
    def __init__(
        self,
        idle_threshold: float,
        window_patterns: List[str],
        idle_commands: List[str],
        poll_interval: float = 5.0,
    ):
        super().__init__(daemon=True, name="IdleMonitor")
        self._stop      = Event()
        self._lock      = Lock()
        self._threshold = idle_threshold
        self._patterns  = window_patterns
        self._commands  = idle_commands
        self._poll      = poll_interval

    def update_config(
        self,
        idle_threshold: Optional[float]     = None,
        window_patterns: Optional[List[str]] = None,
        idle_commands: Optional[List[str]]   = None,
    ) -> None:
        with self._lock:
            if idle_threshold  is not None: self._threshold = idle_threshold
            if window_patterns is not None: self._patterns  = window_patterns
            if idle_commands   is not None: self._commands  = idle_commands
        log.info(
            "IdleMonitor config updated: threshold=%s patterns=%s cmds=%s",
            self._threshold, self._patterns, self._commands,
        )

    def stop(self) -> None:
        self._stop.set()

    # ── actions ───────────────────────────────────────────────────────────────
    def _do_bring_to_front(self, threshold: float, patterns: List[str]) -> None:
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern:
                continue
            hwnds = find_windows_by_title(pattern)
            if not hwnds:
                log.debug("No windows match pattern: %r", pattern)
                continue
            for hwnd in hwnds:
                log.info("Raising hwnd=%s for pattern=%r", hwnd, pattern)
                _growl("info", "KeepOn",
                       f"[idle={threshold}s] Raising hwnd={hwnd} for pattern={pattern!r}")
                bring_to_front_preserve_state(hwnd)

    def _do_run_commands(self, threshold: float, commands: List[str]) -> None:
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd:
                continue
            log.info("Running idle command: %s", cmd)
            _growl("info", "KeepOn", f"Running idle ({threshold}s) command: {cmd}")
            try:
                kwargs: dict = {"shell": True}
                if platform.system() == "Windows":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore
                proc = subprocess.Popen(cmd, **kwargs)  # type: ignore
                log.debug("Spawned pid=%s", proc.pid)
                _growl("warning", "KeepOn", f"Spawned pid={proc.pid}")
            except Exception:
                log.error("Command failed %r:\n%s", cmd, traceback.format_exc())
                _growl("error", "KeepOn",
                       f"Command failed: {cmd}\n{traceback.format_exc()}")

    # ── main loop ─────────────────────────────────────────────────────────────
    def run(self) -> None:
        log.debug("IdleMonitor started")

        # Single state variable — eliminates the dual-boolean race.
        #   "active"     — user is active; watching for idle onset
        #   "idle_fired" — idle actions already fired; suppressed until activity resets
        state = "active"

        while not self._stop.wait(timeout=self._poll):
            # ── Snapshot ALL config atomically ────────────────────────────────
            # One lock scope captures threshold, patterns, and commands together.
            # A hot-reload arriving mid-loop therefore takes full effect on the
            # NEXT cycle rather than half-applying (old threshold + new commands).
            with self._lock:
                threshold   = self._threshold
                patterns    = list(self._patterns)
                commands    = list(self._commands)

            has_windows = any(p.strip() for p in patterns)
            has_cmds    = any(c.strip() for c in commands)

            idle = get_idle_seconds()

            if idle >= threshold:
                if state == "active":
                    log.info("Idle detected: %.1fs >= %.1fs — firing actions", idle, threshold)
                    state = "idle_fired"
                    if has_windows:
                        self._do_bring_to_front(threshold, patterns)
                    if has_cmds:
                        self._do_run_commands(threshold, commands)
                # state == "idle_fired" → already fired; do nothing until activity resets
            else:
                if state == "idle_fired":
                    log.info("Activity resumed (idle dropped to %.1fs < %.1fs)", idle, threshold)
                # Reset unconditionally — any sub-threshold reading re-arms the trigger
                state = "active"

        log.debug("IdleMonitor stopped")


# ─────────────────────────────────────────────────────────────────────────────
# Signals bridge (config reload must touch Qt objects on the main thread)
# ─────────────────────────────────────────────────────────────────────────────
class _Signals(QObject):
    config_changed = pyqtSignal()


# ─────────────────────────────────────────────────────────────────────────────
# Tray application
#
# Key fixes vs original:
#   1. _watcher.fileChanged signal is connected ONCE in __init__, not inside
#      _watch_config_file(), preventing duplicate signal connections.
#   2. _watch_config_file() is called from reload_config() too, so the watcher
#      re-arms itself if the config migrated to a different candidate path.
#   3. SleepPreventer.interval is updated via reload_interval() which is
#      thread-safe and immediately interrupts the current sleep.
# ─────────────────────────────────────────────────────────────────────────────
class TrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        # ── initial config load ───────────────────────────────────────────────
        self.cfg = load_config()
        self._apply_logging_config()

        heartbeat, idle_thresh, win_patterns, idle_cmds = self._parse_config()

        # ── tray icon ─────────────────────────────────────────────────────────
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon(resource_path("icons/heart.ico")))
        self.tray.setVisible(True)
        self.tray.activated.connect(self._on_tray_activated)

        # ── workers ───────────────────────────────────────────────────────────
        self.preventer = SleepPreventer(interval=heartbeat)
        self.preventer.start()

        self.idle_monitor = IdleMonitor(
            idle_threshold=idle_thresh,
            window_patterns=win_patterns,
            idle_commands=idle_cmds,
        )
        self.idle_monitor.start()

        # ── config hot-reload via QFileSystemWatcher ──────────────────────────
        self._signals = _Signals()
        self._signals.config_changed.connect(self._on_config_changed)
        self._watcher = QFileSystemWatcher()

        # Connect the signal ONCE here — never inside _watch_config_file().
        # Connecting inside that method would add a duplicate handler each time
        # it is called (e.g. on reload), causing N callbacks per file event.
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watch_config_file()

        # ── tray menu ─────────────────────────────────────────────────────────
        self.menu = QMenu()

        self.start_action  = QAction(QIcon(resource_path("icons/start.ico")),  "Start")
        self.stop_action   = QAction(QIcon(resource_path("icons/stop.ico")),   "Stop")
        self.reload_action = QAction(QIcon(resource_path("icons/reload.ico")), "Reload Config")
        self.exit_action   = QAction(QIcon(resource_path("icons/exit.ico")),   "Exit")

        self.start_action.triggered.connect(self.start_keep_alive)
        self.stop_action.triggered.connect(self.stop_keep_alive)
        self.reload_action.triggered.connect(self.reload_config)
        self.exit_action.triggered.connect(self.exit_app)

        self.menu.addAction(self.start_action)
        self.menu.addAction(self.stop_action)
        self.menu.addSeparator()
        self.menu.addAction(self.reload_action)
        self.menu.addSeparator()
        self.menu.addAction(self.exit_action)
        self.tray.setContextMenu(self.menu)

        # ── initial state ─────────────────────────────────────────────────────
        auto_start = self.cfg.getboolean("keepon", "prevent_sleep", fallback=True)
        if auto_start:
            self.start_keep_alive(notify=False)
        else:
            self.stop_keep_alive(notify=False)

    # ─────────────────────────────────────────────────────────────────────────
    # Config helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _parse_config(self):
        heartbeat   = self.cfg.getfloat("keepon", "heartbeat_interval", fallback=30)
        idle_thresh = self.cfg.getfloat("idle",   "idle_threshold",     fallback=60)
        raw         = self.cfg.get("idle", "bring_to_front_windows", fallback="")
        win_patterns = [p.strip() for p in raw.split(",") if p.strip()]
        idle_cmds    = self._load_idle_commands()
        return heartbeat, idle_thresh, win_patterns, idle_cmds

    def _load_idle_commands(self) -> List[str]:
        if not self.cfg.has_section("idle_commands"):
            return []
        return [
            v.strip()
            for _, v in self.cfg.items("idle_commands")
            if v.strip() and not v.strip().startswith("#")
        ]

    def _apply_logging_config(self) -> None:
        lvl_str = self.cfg.get("logging", "level", fallback="INFO").upper()
        lvl = getattr(logging, lvl_str, logging.INFO)
        logging.getLogger().setLevel(lvl)

        log_file = self.cfg.get("logging", "file", fallback="").strip()
        if log_file:
            root = logging.getLogger()
            # Remove any existing file handler to avoid duplicates on reload
            for h in root.handlers[:]:
                if isinstance(h, logging.FileHandler):
                    root.removeHandler(h)
                    h.close()
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(lvl)
            fh.setFormatter(logging.Formatter(_LOG_FMT))
            root.addHandler(fh)

    # ─────────────────────────────────────────────────────────────────────────
    # Hot-reload: QFileSystemWatcher
    # ─────────────────────────────────────────────────────────────────────────
    def _watch_config_file(self) -> None:
        """
        Add the current config path to the watcher.
        Called from __init__ and from reload_config() so that if the config
        file migrates to a different candidate path the watcher follows it.

        NOTE: fileChanged is connected once in __init__ — do NOT connect it
        here to avoid accumulating duplicate signal handlers across reloads.
        """
        cfg_path = find_config_path()
        if not cfg_path:
            return
        path_str = str(cfg_path)
        if path_str not in self._watcher.files():
            self._watcher.addPath(path_str)
            log.info("Watching config file: %s", path_str)

    def _on_file_changed(self, path: str) -> None:
        """
        Called by Qt in the main thread when the watched file changes.
        Some editors (vim, Notepad++) do a write-rename which removes the
        old inode; we re-add the path after a short delay to let the rename
        complete before reading.
        """
        log.debug("Config file changed on disk: %s", path)

        def _re_add():
            if path not in self._watcher.files():
                self._watcher.addPath(path)
                log.debug("Re-added watcher for %s", path)
            self._signals.config_changed.emit()

        QTimer.singleShot(200, _re_add)   # 200 ms — enough for rename flush

    def _on_config_changed(self) -> None:
        """Runs in the main thread (via Qt signal) — safe to touch Qt objects."""
        log.info("Hot-reload triggered by file watcher")
        _growl("reload", "KeepOn", "Config file changed — reloading")
        self.reload_config(notify=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Reload
    # ─────────────────────────────────────────────────────────────────────────
    def reload_config(self, notify: bool = True) -> None:
        self.cfg = load_config()
        self._apply_logging_config()

        heartbeat, idle_thresh, win_patterns, idle_cmds = self._parse_config()

        # Thread-safe interval update — interrupts the current sleep immediately
        self.preventer.reload_interval(heartbeat)

        self.idle_monitor.update_config(
            idle_threshold=idle_thresh,
            window_patterns=win_patterns,
            idle_commands=idle_cmds,
        )

        # Re-arm the watcher in case the config path changed
        self._watch_config_file()

        log.info(
            "Reloaded: heartbeat=%ss idle_threshold=%ss patterns=%s commands=%s",
            heartbeat, idle_thresh, win_patterns, idle_cmds,
        )
        if notify:
            self.tray.showMessage(
                "Stay Awake", "Configuration reloaded", QSystemTrayIcon.Information
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Tray actions
    # ─────────────────────────────────────────────────────────────────────────
    def start_keep_alive(self, notify: bool = True) -> None:
        self.preventer.enable()
        self.tray.setIcon(QIcon(resource_path("icons/heart.ico")))
        if notify:
            self.tray.showMessage(
                "Stay Awake", "Prevention started", QSystemTrayIcon.Information
            )
        log.info("Sleep prevention enabled")

    def stop_keep_alive(self, notify: bool = True) -> None:
        self.preventer.disable()
        self.tray.setIcon(QIcon(resource_path("icons/heart_cross.ico")))
        if notify:
            self.tray.showMessage(
                "Stay Awake", "Prevention stopped", QSystemTrayIcon.Information
            )
        log.info("Sleep prevention disabled")

    def exit_app(self) -> None:
        log.info("Exiting")
        self.preventer.stop()
        self.idle_monitor.stop()
        allow_sleep()
        self.tray.setVisible(False)
        self.app.quit()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            if self.preventer.is_active:
                self.stop_keep_alive()
            else:
                self.start_keep_alive()

    def run(self) -> None:
        sys.exit(self.app.exec_())


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    TrayApp().run()
