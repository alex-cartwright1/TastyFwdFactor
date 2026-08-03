"""Qt-native parallel fan-out.

The scan is I/O-bound (HTTP chain requests, yfinance scrapes) and needs a worker
pool, but the app has no `concurrent.futures` anywhere — `QThreadPool` /
`QRunnable` is the Qt equivalent and keeps every thread in the app under one
model.

`parallel_map` blocks the *calling* thread, so it must only ever be called from
a worker thread, never from the GUI thread.
"""

from PySide6.QtCore import QMutex, QMutexLocker, QRunnable, QSemaphore, QThreadPool


class _Task(QRunnable):
    def __init__(self, fn, item, sink):
        super().__init__()
        self._fn, self._item, self._sink = fn, item, sink
        self.setAutoDelete(True)

    def run(self):
        try:
            result = self._fn(self._item)
        except Exception as exc:                  # never let a worker kill the pool
            result = exc
        self._sink.record(self._item, result)


class _Sink:
    def __init__(self):
        self._mutex   = QMutex()
        self._results = []
        self.slots    = QSemaphore(0)

    def record(self, item, result):
        with QMutexLocker(self._mutex):
            self._results.append((item, result))
        self.slots.release()

    def take_all(self):
        with QMutexLocker(self._mutex):
            return list(self._results)


def parallel_map(fn, items, max_workers=10, progress_cb=None, should_cancel=None):
    """Run `fn` over `items` on a private pool, yielding ``(item, result)`` pairs
    in completion order. An exception raised by `fn` is returned as the result
    rather than propagated.

    `progress_cb(done, total)` fires on the calling thread after each
    completion. If `should_cancel()` returns True the remaining queued tasks are
    dropped and only the completed results are returned.
    """
    items = list(items)
    if not items:
        return []

    pool = QThreadPool()
    pool.setMaxThreadCount(max(1, max_workers))
    sink = _Sink()
    for item in items:
        pool.start(_Task(fn, item, sink))

    total = len(items)
    for done in range(1, total + 1):
        sink.slots.acquire()
        if progress_cb:
            progress_cb(done, total)
        if should_cancel and should_cancel():
            # clear() drops queued tasks; already-running ones still finish.
            pool.clear()
            pool.waitForDone()
            break

    return sink.take_all()
