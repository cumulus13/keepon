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

# ─────────────────────────────────────────────────────────────────────────────
# Logging — set up early; level/file overridden after config load
# ─────────────────────────────────────────────────────────────────────────────
_LOG_FMT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.DEBUG, format=_LOG_FMT,
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("keepon")

# ─────────────────────────────────────────────────────────────────────────────
# Windows API — constants
# ─────────────────────────────────────────────────────────────────────────────
ES_CONTINUOUS       = 0x80000000
ES_SYSTEM_REQUIRED  = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

SW_RESTORE          = 9   # un-minimise; maximised → stays maximised
SW_MINIMIZE         = 6
SW_MAXIMIZE         = 3
SW_SHOWNORMAL       = 1

HWND_TOP            = 0   # not a HWND object — pass as c_void_p / int

SWP_NOSIZE          = 0x0001
SWP_NOMOVE          = 0x0002
SWP_NOACTIVATE      = 0x0010
SWP_NOOWNERZORDER   = 0x0200
SWP_SHOWWINDOW      = 0x0040

# ─────────────────────────────────────────────────────────────────────────────
# Windows API — structs  (all integer fields are 32-bit on Windows regardless
# of host arch, so we use c_int32 / c_uint32 explicitly)
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
# (prevents silent truncation of handles on 64-bit)
# ─────────────────────────────────────────────────────────────────────────────
_u32 = ctypes.windll.user32
_k32 = ctypes.windll.kernel32

_u32.GetWindowPlacement.argtypes  = [ctypes.wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
_u32.GetWindowPlacement.restype   = ctypes.wintypes.BOOL
_u32.SetWindowPlacement.argtypes  = [ctypes.wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
_u32.SetWindowPlacement.restype   = ctypes.wintypes.BOOL
_u32.SetWindowPos.argtypes        = [
    ctypes.wintypes.HWND, ctypes.wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_uint,
]
_u32.SetWindowPos.restype         = ctypes.wintypes.BOOL
_u32.ShowWindow.argtypes          = [ctypes.wintypes.HWND, ctypes.c_int]
_u32.ShowWindow.restype           = ctypes.wintypes.BOOL
_u32.BringWindowToTop.argtypes    = [ctypes.wintypes.HWND]
_u32.BringWindowToTop.restype     = ctypes.wintypes.BOOL
_u32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
_u32.SetForegroundWindow.restype  = ctypes.wintypes.BOOL
_u32.GetForegroundWindow.argtypes = []
_u32.GetForegroundWindow.restype  = ctypes.wintypes.HWND
_u32.GetWindowThreadProcessId.argtypes = [
    ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)
]
_u32.GetWindowThreadProcessId.restype  = ctypes.wintypes.DWORD
_u32.AttachThreadInput.argtypes   = [
    ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.BOOL
]
_u32.AttachThreadInput.restype    = ctypes.wintypes.BOOL
_u32.IsWindowVisible.argtypes     = [ctypes.wintypes.HWND]
_u32.IsWindowVisible.restype      = ctypes.wintypes.BOOL
_u32.IsIconic.argtypes            = [ctypes.wintypes.HWND]
_u32.IsIconic.restype             = ctypes.wintypes.BOOL
_u32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
_u32.GetWindowTextLengthW.restype  = ctypes.c_int
_u32.GetWindowTextW.argtypes      = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
_u32.GetWindowTextW.restype       = ctypes.c_int
_u32.EnumWindows.argtypes         = [ctypes.WINFUNCTYPE(ctypes.c_bool,
                                        ctypes.wintypes.HWND, ctypes.py_object),
                                     ctypes.py_object]
_u32.EnumWindows.restype          = ctypes.wintypes.BOOL
_u32.GetLastInputInfo.argtypes    = [ctypes.POINTER(LASTINPUTINFO)]
_u32.GetLastInputInfo.restype     = ctypes.wintypes.BOOL
_k32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
_k32.SetThreadExecutionState.restype  = ctypes.c_uint32
_k32.GetTickCount.argtypes        = []
_k32.GetTickCount.restype         = ctypes.c_uint32
_k32.GetLastError.argtypes        = []
_k32.GetLastError.restype         = ctypes.c_uint32
_k32.GetCurrentThreadId.argtypes  = []
_k32.GetCurrentThreadId.restype   = ctypes.wintypes.DWORD

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
    results = []
    _u32.EnumWindows(_ENUM_PROC(_enum_cb), ctypes.py_object(results))
    pl = pattern.lower()
    return [h for h, t in results if pl in t.lower()]

# ─────────────────────────────────────────────────────────────────────────────
# Bring window to front — state-preserving
#
# Root cause of the original failure:
#   Windows Vista+ LockSetForegroundWindow() blocks cross-process
#   SetForegroundWindow / Z-order changes unless the caller's thread shares
#   input state with the foreground thread.  AttachThreadInput() grants that
#   temporary sharing, making the raise actually work.
#
# State-preservation guarantee:
#   1. GetWindowPlacement captures showCmd (SW_MAXIMIZE / SW_MINIMIZE / SW_SHOWNORMAL)
#      and the saved normal rect before we touch anything.
#   2. SW_RESTORE is called ONLY on minimised windows.  MSDN is explicit:
#      "If the window is maximized, SW_RESTORE has no effect."  So a window
#      that was maximised and then minimised comes back maximised.
#   3. SetWindowPlacement is re-applied at the end as an unconditional
#      guarantee — even if any intermediate call changed showCmd, this
#      restores it to the value captured in step 1.
# ─────────────────────────────────────────────────────────────────────────────
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

        attached_fg     = False
        attached_target = False

        if fg_tid and fg_tid != our_tid:
            attached_fg = bool(_u32.AttachThreadInput(our_tid, fg_tid, True))
        if target_tid and target_tid != our_tid and target_tid != fg_tid:
            attached_target = bool(_u32.AttachThreadInput(our_tid, target_tid, True))

        try:
            # ── 3. Un-minimise if needed (SW_RESTORE is safe; see docstring) ──
            if _u32.IsIconic(hwnd):
                _u32.ShowWindow(hwnd, SW_RESTORE)

            # ── 4. Raise Z-order + show without activation ────────────────────
            _u32.BringWindowToTop(hwnd)
            _u32.SetWindowPos(
                hwnd, HWND_TOP,
                0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE |
                SWP_NOOWNERZORDER | SWP_SHOWWINDOW,
            )

            # ── 5. Actually set foreground (works because threads are attached) 
            _u32.SetForegroundWindow(hwnd)

        finally:
            # ── 6. Always detach, even on exception ───────────────────────────
            if attached_fg:
                _u32.AttachThreadInput(our_tid, fg_tid, False)
            if attached_target:
                _u32.AttachThreadInput(our_tid, target_tid, False)

        # ── 7. Unconditionally restore placement → state cannot have changed ──
        _u32.SetWindowPlacement(hwnd, ctypes.byref(wp))

        log.debug("hwnd=%s raised; showCmd restored to %s", hwnd, original_show_cmd)
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
    path.write_text("""\
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
""", encoding="utf-8")

# ─────────────────────────────────────────────────────────────────────────────
# Sleep preventer thread
# ─────────────────────────────────────────────────────────────────────────────
class SleepPreventer(Thread):
    def __init__(self, interval: float = 30):
        super().__init__(daemon=True, name="SleepPreventer")
        self._stop   = Event()
        self._active = Event()
        self._active.set()
        self.interval = interval

    @property
    def is_active(self) -> bool:
        return self._active.is_set()

    def enable(self)  -> None: self._active.set()
    def disable(self) -> None:
        self._active.clear()
        allow_sleep()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.debug("SleepPreventer started")
        while not self._stop.wait(timeout=self.interval):
            if self._active.is_set():
                prevent_sleep()
                log.debug("heartbeat: sleep prevented")
        allow_sleep()
        log.debug("SleepPreventer stopped")

# ─────────────────────────────────────────────────────────────────────────────
# Idle monitor thread
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
        self._stop         = Event()
        self._lock         = Lock()
        self._threshold    = idle_threshold
        self._patterns     = window_patterns
        self._commands     = idle_commands
        self._poll         = poll_interval
        self._idle_fired   = False

    def update_config(
        self,
        idle_threshold: Optional[float]    = None,
        window_patterns: Optional[List[str]] = None,
        idle_commands: Optional[List[str]] = None,
    ) -> None:
        with self._lock:
            if idle_threshold   is not None: self._threshold = idle_threshold
            if window_patterns  is not None: self._patterns  = window_patterns
            if idle_commands    is not None: self._commands  = idle_commands
        log.info("IdleMonitor config updated: threshold=%s patterns=%s cmds=%s",
                 self._threshold, self._patterns, self._commands)

    def stop(self) -> None:
        self._stop.set()

    # ─────────────────────────────────────────────────────────────────────────
    def _do_bring_to_front(self) -> None:
        with self._lock:
            patterns = list(self._patterns)
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
                bring_to_front_preserve_state(hwnd)

    def _do_run_commands(self) -> None:
        with self._lock:
            commands = list(self._commands)
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd:
                continue
            log.info("Running idle command: %s", cmd)
            try:
                kwargs: dict = {"shell": True}
                if platform.system() == "Windows":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore
                proc = subprocess.Popen(cmd, **kwargs)  # type: ignore
                log.debug("Spawned pid=%s", proc.pid)
            except Exception:
                log.error("Command failed %r:\n%s", cmd, traceback.format_exc())

    def run(self) -> None:
        log.debug("IdleMonitor started")
        was_idle = False
        while not self._stop.wait(timeout=self._poll):
            idle = get_idle_seconds()
            with self._lock:
                threshold   = self._threshold
                has_windows = any(p.strip() for p in self._patterns)
                has_cmds    = any(c.strip() for c in self._commands)

            if idle >= threshold:
                if not was_idle:
                    log.info("Idle detected: %.1fs >= %.1fs", idle, threshold)
                    was_idle = True
                    self._idle_fired = False
                if not self._idle_fired:
                    self._idle_fired = True
                    if has_windows: self._do_bring_to_front()
                    if has_cmds:    self._do_run_commands()
            else:
                if was_idle:
                    log.info("Activity resumed (idle was %.1fs)", idle)
                was_idle = False
                self._idle_fired = False

        log.debug("IdleMonitor stopped")

# ─────────────────────────────────────────────────────────────────────────────
# Signals bridge (config reload must touch Qt objects on the main thread)
# ─────────────────────────────────────────────────────────────────────────────
class _Signals(QObject):
    config_changed = pyqtSignal()

# ─────────────────────────────────────────────────────────────────────────────
# Tray application
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
        self._watch_config_file()

        # ── tray menu ─────────────────────────────────────────────────────────
        self.menu = QMenu()

        self.start_action  = QAction(QIcon(resource_path("icons/start.ico")), "Start")
        self.stop_action   = QAction(QIcon(resource_path("icons/stop.ico")),  "Stop")
        self.reload_action = QAction("Reload Config")
        self.exit_action   = QAction(QIcon(resource_path("icons/exit.ico")),  "Exit")

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
        self.start_keep_alive(notify=False) if auto_start else self.stop_keep_alive(notify=False)

    # ─────────────────────────────────────────────────────────────────────────
    # Config helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _parse_config(self):
        heartbeat    = self.cfg.getfloat("keepon", "heartbeat_interval", fallback=30)
        idle_thresh  = self.cfg.getfloat("idle",   "idle_threshold",     fallback=60)
        raw          = self.cfg.get("idle", "bring_to_front_windows", fallback="")
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
            # Avoid duplicate file handlers on reload
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
        Watch whichever config file is currently loaded.
        QFileSystemWatcher.fileChanged fires when the file is modified or
        replaced (some editors write a new file then rename it — that removes
        the inode, so we re-add the path on the next event).
        """
        cfg_path = find_config_path()
        if not cfg_path:
            return
        path_str = str(cfg_path)
        if path_str not in self._watcher.files():
            self._watcher.addPath(path_str)
            log.info("Watching config file: %s", path_str)
        self._watcher.fileChanged.connect(self._on_file_changed)

    def _on_file_changed(self, path: str) -> None:
        """
        Called by Qt in the main thread when the watched file changes.
        Some editors (vim, Notepad++) do a write-rename which removes the
        old inode — we re-add the path with a short delay to let the rename
        complete before reading.
        """
        log.debug("Config file changed: %s", path)

        # Re-watch in case the file was replaced (rename-based saves)
        def _re_add():
            if path not in self._watcher.files():
                self._watcher.addPath(path)
                log.debug("Re-added watcher for %s", path)
            self._signals.config_changed.emit()

        QTimer.singleShot(200, _re_add)   # 200 ms — enough for rename flush

    def _on_config_changed(self) -> None:
        """Runs in the main thread (via signal) — safe to touch Qt objects."""
        log.info("Hot-reload triggered")
        self.reload_config(notify=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Reload
    # ─────────────────────────────────────────────────────────────────────────
    def reload_config(self, notify: bool = True) -> None:
        self.cfg = load_config()
        self._apply_logging_config()

        heartbeat, idle_thresh, win_patterns, idle_cmds = self._parse_config()

        self.preventer.interval = heartbeat
        self.idle_monitor.update_config(
            idle_threshold=idle_thresh,
            window_patterns=win_patterns,
            idle_commands=idle_cmds,
        )

        log.info(
            "Reloaded: heartbeat=%ss idle_threshold=%ss "
            "patterns=%s commands=%s",
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
