"""The authentication gate.

`LoginWindow` runs before the dashboard exists. It collects the Tastytrade OAuth
grant, verifies it against the live API on a background thread (so the form
never freezes), and only then hands an already-authenticated `ApiSession` to the
main window — which is why the dashboard can assume `session.peek()` is valid
from its first paint.

"Remember me" writes through `config`, which prefers the OS keyring and falls
back to a mode-600 JSON file where no keyring backend exists.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from applog import log
from config import (
    KEYRING_AVAILABLE, clear_credentials, load_credentials, save_credentials,
)
from theme import T, Spinner, make_icon
from workers import ApiSession, AuthWorker

DOCS_HINT = ("my.tastytrade.com → My Profile → Manage → "
             "OAuth Applications → Create Grant")


class LoginWindow(QDialog):
    """Modal sign-in. `exec()` returns Accepted once the grant is verified."""

    def __init__(self, session: ApiSession = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sign in — TastyFwdFactor")
        self.setModal(True)
        self.setFixedSize(880, 560)

        self.session = session or ApiSession(self)
        self._worker = None

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_brand_panel(), 1)
        root.addWidget(self._build_form_panel(), 1)

        self._restore_saved()

    # ── left: brand panel ────────────────────────────────────────────────────

    def _build_brand_panel(self):
        panel = QWidget()
        panel.setObjectName("BrandPanel")
        panel.setStyleSheet(f"""
            #BrandPanel {{
                background-color: {T.SURFACE};
                border-right: 1px solid {T.BORDER_SOFT};
            }}
        """)
        box = QVBoxLayout(panel)
        box.setContentsMargins(36, 36, 36, 36)
        box.setSpacing(14)

        logo = QLabel()
        logo.setPixmap(make_icon('logo', T.ACCENT, 44).pixmap(44, 44))
        box.addWidget(logo)
        box.addSpacing(6)

        title = QLabel("TastyFwdFactor")
        title.setProperty("role", "title")
        title.setStyleSheet("font-size: 26px; font-weight: 600; line-height: 130%;")
        box.addWidget(title)

        blurb = QLabel(
            "Rank calendar spreads by forward factor across your watchlist, "
            "then track open positions with live P/L.")
        blurb.setWordWrap(True)
        blurb.setProperty("role", "subtitle")
        box.addWidget(blurb)
        box.addStretch(1)

        for text in ("Live DXLink streaming quotes",
                     "Earnings & dividend aware filtering",
                     "Interactive P/L modelling"):
            box.addWidget(self._bullet(text))

        box.addStretch(1)
        footer = QLabel("Read-only. This app never places orders.")
        footer.setProperty("role", "hint")
        box.addWidget(footer)
        return panel

    @staticmethod
    def _bullet(text):
        row = QWidget()
        # A bare QWidget picks up the window background from the global sheet,
        # which would paint a block over the brand panel's lighter surface.
        row.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        dot = QLabel()
        dot.setFixedSize(6, 6)
        dot.setStyleSheet(f"background-color: {T.ACCENT}; border-radius: 3px;")
        layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)

        label = QLabel(text)
        label.setStyleSheet(f"color: {T.TEXT_DIM};")
        layout.addWidget(label)
        layout.addStretch(1)
        return row

    # ── right: form ──────────────────────────────────────────────────────────

    def _build_form_panel(self):
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(40, 40, 40, 32)
        box.setSpacing(0)

        heading = QLabel("Sign in")
        heading.setProperty("role", "title")
        box.addWidget(heading)

        sub = QLabel(f"Enter your Tastytrade OAuth grant.\n{DOCS_HINT}")
        sub.setProperty("role", "subtitle")
        sub.setWordWrap(True)
        box.addWidget(sub)
        box.addSpacing(24)

        self.secret_edit = self._field(box, "CLIENT SECRET", "Client secret")
        box.addSpacing(14)
        self.refresh_edit = self._field(box, "REFRESH TOKEN", "Refresh token")
        box.addSpacing(16)

        options = QHBoxLayout()
        self.remember = QCheckBox("Remember me")
        self.remember.setChecked(True)
        options.addWidget(self.remember)
        options.addStretch(1)
        self.reveal_btn = QPushButton("Show")
        self.reveal_btn.setProperty("variant", "ghost")
        self.reveal_btn.setCheckable(True)
        self.reveal_btn.toggled.connect(self._toggle_reveal)
        options.addWidget(self.reveal_btn)
        box.addLayout(options)

        if not KEYRING_AVAILABLE:
            warn = QLabel("No OS keyring available — credentials will be stored "
                          "in a permission-restricted JSON file.")
            warn.setWordWrap(True)
            warn.setProperty("role", "hint")
            warn.setStyleSheet(f"color: {T.WARNING};")
            box.addSpacing(8)
            box.addWidget(warn)

        box.addSpacing(20)
        self.sign_in_btn = QPushButton("Sign in")
        self.sign_in_btn.setProperty("variant", "primary")
        self.sign_in_btn.setMinimumHeight(38)
        self.sign_in_btn.setDefault(True)
        self.sign_in_btn.clicked.connect(self._attempt_login)
        box.addWidget(self.sign_in_btn)

        # Status strip: spinner + message, hidden until something happens.
        box.addSpacing(14)
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.spinner = Spinner(18, T.ACCENT)
        status_row.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("role", "hint")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Preferred)
        status_row.addWidget(self.status_label, 1)
        box.addLayout(status_row)

        box.addStretch(1)

        quit_btn = QPushButton("Quit")
        quit_btn.setProperty("variant", "ghost")
        quit_btn.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(quit_btn)
        box.addLayout(bottom)

        for edit in (self.secret_edit, self.refresh_edit):
            edit.returnPressed.connect(self._attempt_login)
            edit.textEdited.connect(self._clear_error)
        return panel

    @staticmethod
    def _field(layout, label_text, placeholder):
        label = QLabel(label_text)
        label.setProperty("role", "section")
        layout.addWidget(label)
        layout.addSpacing(6)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setMinimumHeight(36)
        layout.addWidget(edit)
        return edit

    # ── behaviour ────────────────────────────────────────────────────────────

    def _restore_saved(self):
        secret, refresh = load_credentials()
        self.secret_edit.setText(secret)
        self.refresh_edit.setText(refresh)
        if secret and refresh:
            self._set_status("Saved credentials loaded.", tone=None)
            self.sign_in_btn.setFocus()
        else:
            self.secret_edit.setFocus()

    def _toggle_reveal(self, revealed):
        mode = (QLineEdit.EchoMode.Normal if revealed
                else QLineEdit.EchoMode.Password)
        self.secret_edit.setEchoMode(mode)
        self.refresh_edit.setEchoMode(mode)
        self.reveal_btn.setText("Hide" if revealed else "Show")

    def _clear_error(self, *_):
        for edit in (self.secret_edit, self.refresh_edit):
            edit.setProperty("state", "")
            self._repolish(edit)
        if self._worker is None:
            self._set_status("", tone=None)

    def _set_status(self, message, tone="hint"):
        colour = {
            "error":   T.NEGATIVE,
            "success": T.POSITIVE,
            "hint":    T.TEXT_MUTED,
        }.get(tone, T.TEXT_MUTED)
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {colour}; font-size: 11px;")

    @staticmethod
    def _repolish(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _mark_invalid(self, *edits):
        for edit in edits:
            edit.setProperty("state", "error")
            self._repolish(edit)

    def _set_busy(self, busy):
        self.sign_in_btn.setEnabled(not busy)
        self.sign_in_btn.setText("Signing in…" if busy else "Sign in")
        self.secret_edit.setEnabled(not busy)
        self.refresh_edit.setEnabled(not busy)
        self.remember.setEnabled(not busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def _attempt_login(self):
        if self._worker is not None:
            return
        secret  = self.secret_edit.text().strip()
        refresh = self.refresh_edit.text().strip()

        missing = [e for e, v in ((self.secret_edit, secret),
                                  (self.refresh_edit, refresh)) if not v]
        if missing:
            self._mark_invalid(*missing)
            self._set_status("Both the client secret and refresh token are "
                             "required.", tone="error")
            missing[0].setFocus()
            return

        self.session.set_credentials(secret, refresh)
        self._set_busy(True)
        self._set_status("Authenticating with Tastytrade…", tone="hint")

        worker = AuthWorker(self.session, parent=self)
        worker.succeeded.connect(self._on_auth_ok)
        worker.failed.connect(self._on_auth_failed)
        worker.finished.connect(self._on_worker_done)
        self._worker = worker
        worker.start()

    def _on_worker_done(self):
        self._worker = None

    def _on_auth_ok(self):
        secret  = self.secret_edit.text().strip()
        refresh = self.refresh_edit.text().strip()
        if self.remember.isChecked():
            save_credentials(secret, refresh)
        else:
            clear_credentials()

        self._set_busy(False)
        self._set_status("Authenticated — opening dashboard…", tone="success")
        log.info("Login successful")
        # A beat on the success state, so the transition doesn't feel like a glitch.
        QTimer.singleShot(320, self.accept)

    def _on_auth_failed(self, message):
        self._set_busy(False)
        self._mark_invalid(self.secret_edit, self.refresh_edit)
        self._set_status(message, tone="error")

    # ── teardown ─────────────────────────────────────────────────────────────

    def reject(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)
        super().reject()

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)
        super().closeEvent(event)
