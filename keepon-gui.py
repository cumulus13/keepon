import sys
import ctypes
import time
from threading import Thread, Event
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction
from PyQt5.QtGui import QIcon

# Constants untuk Windows API
ES_CONTINUOUS       = 0x80000000
ES_SYSTEM_REQUIRED  = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

def prevent_sleep():
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)

def allow_sleep():
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

class SleepPreventer(Thread):
    def __init__(self):
        super().__init__()
        self._stop_event = Event()
        self._running = False

    def run(self):
        self._running = True
        while not self._stop_event.is_set():
            if self._running:
                prevent_sleep()
            time.sleep(30)

    def stop(self):
        self._stop_event.set()

    def start_prevention(self):
        self._running = True

    def pause_prevention(self):
        self._running = False
        allow_sleep()

class TrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon("icons/heart.ico"))  # Ganti dengan path ikonmu jika ingin custom
        self.tray.setVisible(True)

        # Worker thread
        self.preventer = SleepPreventer()
        self.preventer.start()

        # Menu
        self.menu = QMenu()
        self.start_action = QAction(QIcon("icons/start.ico"), "Start")
        self.stop_action = QAction(QIcon("icons/stop.ico"), "Stop")
        self.exit_action = QAction(QIcon("icons/exit.ico"), "Exit")

        self.start_action.triggered.connect(self.start_keep_alive)
        self.stop_action.triggered.connect(self.stop_keep_alive)
        self.exit_action.triggered.connect(self.exit_app)

        self.menu.addAction(self.start_action)
        self.menu.addAction(self.stop_action)
        self.menu.addSeparator()
        self.menu.addAction(self.exit_action)

        self.start_keep_alive()

        self.tray.setContextMenu(self.menu)

    def start_keep_alive(self):
        self.preventer.start_prevention()
        self.tray.showMessage("Stay Awake", "Prevention Started", QSystemTrayIcon.Information)

    def stop_keep_alive(self):
        self.preventer.pause_prevention()
        self.tray.showMessage("Stay Awake", "Prevention Stopped", QSystemTrayIcon.Information)

    def exit_app(self):
        self.preventer.stop()
        allow_sleep()
        self.tray.setVisible(False)
        self.app.quit()

    def run(self):
        sys.exit(self.app.exec_())

if __name__ == "__main__":
    TrayApp().run()
