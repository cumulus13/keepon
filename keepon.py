import ctypes
import time

# Constants dari WinBase.h
ES_CONTINUOUS       = 0x80000000
ES_SYSTEM_REQUIRED  = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# Aktifkan mode keep-awake
def prevent_sleep():
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)

# Kembalikan ke default
def allow_sleep():
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

if __name__ == "__main__":
    try:
        print("keep on. press Ctrl+C for exit/quit.")
        while True:
            prevent_sleep()
            time.sleep(30)  # Perbarui status setiap 30 detik
    except KeyboardInterrupt:
        print("keep on terminated, back to normal mode.")
        allow_sleep()
