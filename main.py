"""main.py - Entry point principale dell'applicazione"""
import sys
from PyQt5.QtWidgets import QApplication
from core.ui_main_window import MainWindow
from core.utils import apply_dark_theme


def main():
    """Entry point principale"""
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
