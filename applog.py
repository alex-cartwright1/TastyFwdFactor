"""Application logging.

Writes to `debug.log` (overwritten on every launch) and re-emits every record as
a Qt signal so the Debug Log tab can append it. The signal is the only channel
into the GUI — emitting from a worker or WebSocket thread is safe because Qt
queues cross-thread deliveries onto the receiver's event loop.
"""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

LOG_PATH = Path(__file__).parent / "debug.log"

_file_logger = logging.getLogger("cal_spread")
_file_logger.setLevel(logging.DEBUG)
if not _file_logger.handlers:
    _fh = logging.FileHandler(LOG_PATH, mode='w', encoding='utf-8')
    _fh.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S'))
    _file_logger.addHandler(_fh)


class AppLog(QObject):
    """Thread-safe logger. `messageLogged` carries (level, message)."""

    messageLogged = Signal(str, str)

    def _write(self, level, msg):
        getattr(_file_logger, level)(msg)
        self.messageLogged.emit(level.upper()[:4], str(msg))

    def debug(self, msg):   self._write('debug',   msg)
    def info(self, msg):    self._write('info',    msg)
    def warning(self, msg): self._write('warning', msg)
    def error(self, msg):   self._write('error',   msg)


log = AppLog()
