"""TastyFwdFactor — Tastytrade production.

Entry point. Everything else lives in:

    applog.py       logging → debug.log + the Logs view
    config.py       settings / credentials / caches on disk
    data_models.py  Quote, ChainInfo, ScanResult, Position
    pricing.py      Black-Scholes, IV solving, forward factor, P/L curves
    api.py          Tastytrade REST + DXLink (one-shot and streaming)
    scanner.py      the scan pipeline
    positions.py    position P/L bookkeeping
    qtpool.py       QThreadPool fan-out
    workers.py      QThread workers (auth, scan, chain resolve) + API session
    live.py         live-quote fan-out and update coalescing
    models.py       QAbstractTableModel for each table
    theme.py        design tokens, global stylesheet, painted icons, spinner
    delegates.py    cell painting: signed colours + live-update flash
    login.py        the authentication gate
    dialogs.py      settings and add/edit-position dialogs
    charts.py       PyQtGraph P/L chart
    main_window.py  the QMainWindow controller
"""

import sys

from PySide6.QtWidgets import QApplication

from login import LoginWindow
from main_window import MainWindow
from theme import apply_theme
from workers import ApiSession


def main():
    # Reuse an existing instance so the app can also be driven from a harness
    # that owns the QApplication (headless smoke tests, embedding).
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("TastyFwdFactor")
    app.setOrganizationName("calendar-spread")
    apply_theme(app)

    # Loop so "Sign out" returns to the login screen instead of exiting. Each
    # pass gets a fresh ApiSession, so a new grant can't inherit the old token.
    while True:
        session = ApiSession()
        login = LoginWindow(session)
        if login.exec() != LoginWindow.DialogCode.Accepted:
            return 0

        window = MainWindow(session)
        signed_out = {'value': False}
        window.signOutRequested.connect(
            lambda: signed_out.__setitem__('value', True))
        # Covers quits that bypass closeEvent (Ctrl-C, session logout): a DXLink
        # thread left in run_forever would keep the process alive.
        app.aboutToQuit.connect(window.shutdown)
        window.show()

        code = app.exec()
        app.aboutToQuit.disconnect(window.shutdown)
        if not signed_out['value']:
            return code


if __name__ == "__main__":
    sys.exit(main())
