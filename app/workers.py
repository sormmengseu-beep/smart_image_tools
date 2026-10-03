from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot


class TaskWorker(QObject):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable) -> None:
        super().__init__()
        self.operation = operation

    @Slot()
    def run(self) -> None:
        try:
            self.succeeded.emit(self.operation(self.progress.emit))
        except Exception as exc:
            logging.getLogger(__name__).exception("File operation failed")
            self.failed.emit(str(exc))
