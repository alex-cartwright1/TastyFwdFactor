"""Calendar Spread Edge Screener — Tastytrade production.

Entry point. Everything else lives in:

    applog.py       logging → debug.log + the Debug Log tab
    config.py       settings / credentials / caches on disk
    data_models.py  Quote, ChainInfo, ScanResult, Position
    pricing.py      Black-Scholes, IV solving, forward factor, P/L curves
    api.py          Tastytrade REST + DXLink (one-shot and streaming)
    scanner.py      the scan pipeline
    positions.py    position P/L bookkeeping
    qtpool.py       QThreadPool fan-out
    workers.py      QThread workers (scan, chain resolve) + shared API session
    live.py         live-quote fan-out and update coalescing
    models.py       QAbstractTableModel for each table
    dialogs.py      settings and add/edit-position dialogs
    charts.py       PyQtGraph P/L chart
    main_window.py  the QMainWindow controller
"""

import sys

from PySide6.QtWidgets import QApplication

from main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Calendar Spread Edge Screener")
    app.setOrganizationName("calendar-spread")
    app.setStyle("Fusion")

    window = MainWindow()
    # Covers quits that bypass closeEvent (Ctrl-C, session logout): a DXLink
    # thread left in run_forever would keep the process alive.
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
