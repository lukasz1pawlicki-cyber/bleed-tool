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

    # Wyczysc cache przy starcie — gwarantuje ze operator zawsze widzi
    # wyniki z aktualnego kodu algorytmu. Koszt: pierwsze przetwarzanie
    # pliku nieco wolniejsze (cache miss), ale nastepne w tej samej sesji
    # trafiaja cache normalnie.
    try:
        from modules.cache import clear_all, is_cache_enabled
        if is_cache_enabled():
            n = clear_all()
            if n > 0:
                print(f"[startup] Wyczyszczono {n} wpisow cache")
    except Exception as e:
        print(f"[startup] cache clear skipped: {e}")

    app = QApplication(sys.argv)
    app.setApplicationName("Bleed Tool")
    load_theme(app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
