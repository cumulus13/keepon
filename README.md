# KeepOn GUI

KeepOn GUI is a Python application that prevents your computer from going to sleep or turning off the display. It runs as a system tray application, allowing you to easily start or stop the sleep prevention functionality.

## Features

- Prevents the system from sleeping or turning off the display.
- Runs in the system tray for easy access.
- Start, stop, or exit the application via a context menu.
- Notifications to indicate the current status of the sleep prevention.

## Requirements (*.py)

- Python 3.x 
- PyQt5

## Installation

1. Clone or download this repository.
2. Install the required dependencies using pip:

   ```sh
   pip install PyQt5
   ```

3. Run the application:

   ```sh
   #gui app
   python keepon-gui.py
   #or console app
   python keepon.py
   #or you can run file *.exe (gui app ~ download from release page)
   keepon.exe
   ```

## Usage

1. When the application starts, it will appear in the system tray with a heart icon.
2. Right-click the tray icon to open the context menu.
3. Use the following options:
   - **Start**: Start preventing the system from sleeping.
   - **Stop**: Stop the sleep prevention and allow the system to sleep normally.
   - **Exit**: Exit the application and restore the system's default sleep behavior.

## File Structure

- `keepon-gui.py`: The main script for the GUI application.
- `keepon.py`: The main script for the console application.
- `icons/`: Contains the icons used for the system tray and menu actions.
  - `heart.ico`: Default tray icon.
  - `start.ico`: Icon for the "Start" action.
  - `stop.ico`: Icon for the "Stop" action.
  - `exit.ico`: Icon for the "Exit" action.

## How It Works

The application uses the Windows API to prevent the system from sleeping by calling `SetThreadExecutionState` with specific flags:

- `ES_CONTINUOUS`: Ensures the state is maintained until explicitly changed.
- `ES_SYSTEM_REQUIRED`: Prevents the system from sleeping.
- `ES_DISPLAY_REQUIRED`: Prevents the display from turning off.

A background thread (`SleepPreventer`) periodically updates the state every 30 seconds to ensure the system remains awake.

## License

This project is licensed under the MIT License.


## Author
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

## Coffee
[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)

[Support me on Patreon](https://www.patreon.com/cumulus13)