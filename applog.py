"""Application logging.

Writes to `debug.log` (overwritten on every launch) and re-emits every record as
a Qt signal so the Debug Log tab can append it. The signal is the only channel
into the GUI — emitting from a worker or WebSocket thread is safe because Qt
queues cross-thread deliveries onto the receiver's event loop.

The log lives in ``CONFIG_DIR`` alongside the rest of the user state, **not**
next to the source. A frozen build's code directory is read-only — inside a
macOS ``.app`` bundle, or a PyInstaller onefile temp dir that is deleted on
exit — so writing there either fails outright or throws the log away.

``CONFIG_DIR`` is defined here rather than in :mod:`config` because config
imports this module for `log`; owning the path at the bottom of the import
graph is what keeps that from becoming a cycle.
"""

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Signal

CONFIG_DIR = Path.home() / ".config" / "calendar-spread"
LOG_PATH   = CONFIG_DIR / "debug.log"

_file_logger = logging.getLogger("cal_spread")
_file_logger.setLevel(logging.DEBUG)
if not _file_logger.handlers:
    _fmt = logging.Formatter(
        '%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S')
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _fh = logging.FileHandler(LOG_PATH, mode='w', encoding='utf-8')
        _fh.setFormatter(_fmt)
        _file_logger.addHandler(_fh)
    except OSError as exc:
        # An unwritable home directory must not stop the app from starting —
        # the in-app Debug Log tab is fed by the signal, not by this handler.
        _sh = logging.StreamHandler(sys.stderr)
        _sh.setFormatter(_fmt)
        _file_logger.addHandler(_sh)
        _file_logger.warning(f"Could not open {LOG_PATH} ({exc}) — logging to stderr")


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
