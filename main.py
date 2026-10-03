from __future__ import annotations

import argparse
import sys

from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QStandardPaths

from app.main_window import MainWindow
from utils.logger import configure_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="Smart File Renamer")
    parser.add_argument("--names", type=Path, help="TXT names file or CSV filename mapping")
    parser.add_argument("--folder", type=Path, help="Folder containing files to rename")
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    app.setApplicationName("SmartFileRenamer")
    app.setApplicationDisplayName("Smart File Renamer")
    configure_logging(Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)) / "logs")
    window = MainWindow()
    window.show()
    if args.names:
        window.names_input.setText(str(args.names.resolve()))
    if args.folder:
        window.folder_input.setText(str(args.folder.resolve()))
    window._update_actions()
    if args.names and args.folder:
        QTimer.singleShot(0, window.preview)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
