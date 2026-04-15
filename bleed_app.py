"""
Bleed Tool — Entry Point (PyQt6)
===================================
Uruchamia GUI na PyQt6 z QSS theming.
"""

import sys
import os
import ctypes

# Windows: DPI awareness
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

# Windows: ukryj konsolę
if sys.platform == "win32":
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def main():
    from PyQt6.QtWidgets import QApplication
    from gui.theme import load_theme
    from gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Bleed Tool")
    load_theme(app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
