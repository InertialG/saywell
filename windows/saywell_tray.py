#!/usr/bin/env python3
"""saywell_tray: saywell on Windows as a tray app: a global hotkey opens the popup over the window
you were typing in, and the version you insert is pasted back there.

  - one instance per user; starting it again opens the popup of the running one
  - hotkey (default Win+Alt+E, `hotkey` under [windows] in config.toml) remembers the foreground
    window and opens the popup frameless, on top, centered on the screen with the mouse
  - pressing the hotkey while the popup is open brings it back to the front
  - insert (Ctrl+Shift+Enter): the text is on the clipboard; focus goes back to the remembered window
    and the paste keys are sent: Ctrl+Shift+V in terminals, Ctrl+V elsewhere (see saywell_win)
  - `saywell --learn` every hour (`learn_interval_minutes`); failures go to the log, quietly
  - tray menu: open, learn now, start at logon, quit
  - log: $XDG_DATA_HOME/saywell/tray.log

Installed:  uv tool install <repo>  ->  saywell-tray [--backend PATH], saywell-autostart enable|disable|status
            (saywell_app calls main(); at logon it runs saywell-tray.exe)
Checkout:   uv run --no-project --with pyside6-essentials pythonw windows/saywell_tray.py [--backend PATH]
Autostart:  ... python windows/saywell_tray.py autostart enable|disable|status [--command CMD]
"""

import argparse
import logging
import logging.handlers
import os
import sys
import time

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QProcess, QProcessEnvironment, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

import saywell_popup_qt as popup_qt
import saywell_win as sw
import win32

HOTKEY_ID = 1
LEARN_TIMEOUT_MS = 20 * 60 * 1000
# Foreground windows that are no place to paste into: the desktop and the taskbar.
NOT_TARGETS = {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"}
log = logging.getLogger("saywell-tray")


def backend_argv(path=None):
    """The backend command for the tray. Run as a pythonw app, the tray has no console, and a
    console program it starts gets CREATE_NO_WINDOW from QProcess: a hidden console that the
    backend's claude.exe inherits. Run with pythonw itself, the backend would have no console and
    every claude.exe it starts would open a console window. So the backend runs on python.exe."""
    cmd = popup_qt.backend_command(path)
    if cmd[0] == sys.executable:
        cmd[0] = str(sw.console_python(sys.executable))
    return cmd


def backend_process(parent, argv):
    """A QProcess for the backend with UTF-8 IO, as the popup starts it."""
    proc = QProcess(parent)
    env = QProcessEnvironment.systemEnvironment()
    env.insert("PYTHONUTF8", "1")
    env.insert("PYTHONIOENCODING", "utf-8")
    proc.setProcessEnvironment(env)
    proc.setProgram(argv[0])
    proc.setArguments(argv[1:])
    return proc


class HotkeyFilter(QAbstractNativeEventFilter):
    """Calls back on WM_HOTKEY for our id. RegisterHotKey without a window posts it to the thread
    queue, which Qt's event dispatcher hands to native filters as windows_dispatcher_MSG."""

    def __init__(self, hotkey_id, callback):
        super().__init__()
        self.hotkey_id, self.callback = hotkey_id, callback

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) in (b"windows_dispatcher_MSG", b"windows_generic_MSG"):
            msg = win32.message_at(message)
            if msg.message == win32.WM_HOTKEY and msg.wParam == self.hotkey_id:
                QTimer.singleShot(0, self.callback)  # open windows outside the native filter
                return True, 0
        return False, 0


class Learner(QObject):
    """Runs `saywell --learn` on a timer and on demand, one at a time."""

    finished = Signal(bool, str, bool)  # ok, message, started by hand

    def __init__(self, backend, interval_minutes, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.proc = None
        self.manual = False
        self.timer = QTimer(self, timeout=lambda: self.run(False))
        if interval_minutes:
            self.timer.start(round(interval_minutes * 60_000))
        self.guard = QTimer(self, singleShot=True, interval=LEARN_TIMEOUT_MS, timeout=self.on_timeout)

    def run(self, manual=False):
        if self.proc:
            log.info("learn: still running, not starting another")
            if manual:
                self.finished.emit(False, "learn is already running", True)
            return False
        log.info("learn: starting (%s)", "by hand" if manual else "scheduled")
        self.manual = manual
        proc = self.proc = backend_process(self, self.backend + ["--learn"])
        proc.finished.connect(lambda code, status: self.on_finished(proc, code == 0 and status == QProcess.NormalExit))
        proc.errorOccurred.connect(lambda err: err == QProcess.FailedToStart and self.on_finished(proc, False))
        self.guard.start()
        proc.start()
        proc.closeWriteChannel()
        return True

    def on_timeout(self):
        if self.proc:
            log.warning("learn: no result after %d min, stopping it", LEARN_TIMEOUT_MS // 60_000)
            self.kill()

    def stop(self):
        """On quit: no more runs, and the one in flight goes with its whole process tree."""
        self.timer.stop()
        if self.proc:
            log.info("learn: stopping the running learn on quit")
            self.kill()

    def kill(self):
        # The backend's claude.exe goes too; on_finished then reports the run and frees the slot.
        proc = self.proc
        popup_qt.kill_tree(proc)
        proc.waitForFinished(5000)

    def on_finished(self, proc, ok):
        if proc is not self.proc:
            return
        self.proc = None
        self.guard.stop()
        decode = lambda b: bytes(b).decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
        out, err = decode(proc.readAllStandardOutput()), decode(proc.readAllStandardError())
        if ok:
            message = out.splitlines()[-1] if out else "done"
            log.info("learn: %s", message)
        else:
            message = err or proc.errorString() or f"exit code {proc.exitCode()}"
            log.warning("learn failed: %s", message)
        self.finished.emit(ok, message, self.manual)
        proc.deleteLater()


class Paster(QObject):
    """Puts focus back on a window and sends it the paste keys, once the user lets go of the
    modifiers of Ctrl+Shift+Enter (else they would join the paste keys).

    One paste at a time: a new paste() supersedes the one in flight, whose pending steps then
    stop without sending anything (the clipboard already holds the newer text)."""

    done = Signal(bool, str)  # pasted, what happened
    POLL_MS, WAIT_S, SETTLE_MS = 15, 1.0, 40

    def __init__(self, win, settings, parent=None):
        super().__init__(parent)
        self.win, self.settings = win, settings
        self.job = None  # the paste in flight: {"hwnd", "deadline"}; steps check they still own it

    def paste(self, hwnd):
        if self.job:
            log.info("paste: into %#x superseded by a new one into %#x", self.job["hwnd"], hwnd)
        job = self.job = {"hwnd": hwnd, "deadline": time.monotonic() + self.WAIT_S}
        QTimer.singleShot(0, lambda: self.focus(job))

    def cancel(self):
        self.job = None

    def finish(self, job, ok, message):
        self.job = None
        self.done.emit(ok, message)

    def focus(self, job):
        if job is not self.job:
            return
        if not self.win.is_window(job["hwnd"]):
            self.finish(job, False, "The window is gone; the text is on the clipboard.")
        elif not self.win.focus_window(job["hwnd"]):
            self.finish(job, False, "Could not switch back to the window; the text is on the clipboard.")
        else:
            self.wait_for_modifiers(job)

    def wait_for_modifiers(self, job):
        if job is not self.job:
            return
        held = self.win.keys_down(win32.MODIFIER_KEYS)
        if held and time.monotonic() < job["deadline"]:
            QTimer.singleShot(self.POLL_MS, lambda: self.wait_for_modifiers(job))
            return
        if held:  # still down after a second: lift them, masked so Alt or Win opens no menu
            log.info("paste: lifting held modifiers %s", [hex(vk) for vk in held])
            events = [(win32.VK_MASK, False, False), (win32.VK_MASK, True, False)]
            self.win.send_keys(events + [(vk, True, False) for vk in held])
        QTimer.singleShot(self.SETTLE_MS, lambda: self.send(job))

    def send(self, job):
        if job is not self.job:
            return
        hwnd = job["hwnd"]
        if self.win.foreground_window() != hwnd:
            self.finish(job, False, "Focus moved to another window; the text is on the clipboard.")
            return
        spec = sw.paste_keys(self.win, hwnd, self.settings)
        events = sw.key_events(spec)
        if self.win.send_keys(events) != len(events):
            self.finish(job, False, f"Sending {spec} failed; the text is on the clipboard.")
        else:
            self.finish(job, True, spec)


class Tray(QObject):
    def __init__(self, win, settings, backend, parent=None):
        super().__init__(parent)
        self.win, self.settings, self.backend = win, settings, backend
        self.popup = None
        self.target = 0           # the window to paste into, remembered when the popup opened
        self.hotkey_ok = False
        self.boxes = []           # open message boxes
        self.paster = Paster(win, settings, self)
        self.paster.done.connect(self.on_pasted)
        self.learner = Learner(backend, settings["learn_interval_minutes"], self)
        self.learner.finished.connect(self.on_learned)
        self.filter = HotkeyFilter(HOTKEY_ID, self.on_hotkey)

        hotkey = settings["hotkey"]
        self.icon = QSystemTrayIcon(make_icon(), self)
        self.icon.setToolTip(f"Saywell · {hotkey}")
        self.menu = QMenu()
        self.menu.addAction(f"Open Saywell\t{hotkey}", self.open_from_menu)
        self.menu.addAction("Learn now", lambda: self.learner.run(True))
        self.autostart_action = QAction("Start at logon", self.menu, checkable=True)
        self.autostart_action.toggled.connect(self.set_autostart)
        self.menu.addAction(self.autostart_action)
        self.menu.addSeparator()
        self.menu.addAction("Quit", QApplication.instance().quit)
        self.menu.aboutToShow.connect(self.sync_autostart)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self.on_icon)

    # ---- start / stop -------------------------------------------------------

    def start(self):
        self.sync_autostart()
        self.icon.show()
        QApplication.instance().installNativeEventFilter(self.filter)
        self.register_hotkey()

    def register_hotkey(self):
        hotkey = self.settings["hotkey"]
        mods, vk = sw.hotkey_args(hotkey)
        err = self.win.register_hotkey(HOTKEY_ID, mods, vk)
        self.hotkey_ok = not err
        if self.hotkey_ok:
            log.info("hotkey %s registered", hotkey)
            return
        why = ("another program already uses it" if err == win32.ERROR_HOTKEY_ALREADY_REGISTERED
               else f"Windows error {err}")
        message = (f"The hotkey {hotkey} could not be registered: {why}.\n\n"
                   f"Pick another one with `hotkey` under [windows] in {sw.CONFIG_PATH} and restart Saywell. "
                   "Until then, open the popup from the tray icon.")
        log.error("hotkey %s not registered: %s", hotkey, why)
        self.icon.setToolTip(f"Saywell · hotkey {hotkey} unavailable")
        self.warn(message)

    def stop(self):
        if self.hotkey_ok:
            self.win.unregister_hotkey(HOTKEY_ID)
            self.hotkey_ok = False
        QApplication.instance().removeNativeEventFilter(self.filter)
        self.paster.cancel()
        self.learner.stop()
        if self.popup:
            self.popup.close()
        self.icon.hide()

    def warn(self, message):
        box = QMessageBox(QMessageBox.Warning, "Saywell", message)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.finished.connect(lambda _: self.boxes.remove(box))
        self.boxes.append(box)
        box.open()

    # ---- popup ----------------------------------------------------------------

    def popup_open(self):
        return self.popup is not None and self.popup.isVisible()

    def is_target(self, hwnd):
        return (bool(hwnd) and self.win.window_pid(hwnd) != os.getpid()
                and self.win.window_class(hwnd).lower() not in NOT_TARGETS)

    def on_hotkey(self):
        """Hotkey (or a second instance): remember where to paste, then show the popup. Pressed
        from another window while the popup is open, it pastes there instead; pressed from the
        popup itself (or the desktop), the earlier window stays the target."""
        fg = self.win.foreground_window()
        if self.is_target(fg) or not self.popup_open():
            self.target = fg if self.is_target(fg) else 0
            log.info("popup for window %#x (%s)", self.target,
                     ", ".join(sorted(sw.window_names(self.win, self.target))) if self.target else "none")
        self.show_popup()

    def open_from_menu(self):
        if not self.popup_open():
            self.target = 0  # the menu took the focus; the text goes to the clipboard only
        self.show_popup()

    def on_icon(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.open_from_menu()

    def show_popup(self):
        # A paste still waiting for the modifiers would find the popup in front, not its window.
        self.paster.cancel()
        if not self.popup_open():
            if self.popup is not None:
                self.popup.deleteLater()
            popup = self.popup = popup_qt.Popup(self.backend)
            popup.setWindowFlags(popup.windowFlags() | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            popup.setStyleSheet(popup.styleSheet() + "#popup { border: 1px solid #3a3a42; }")
            popup.inserted.connect(self.on_inserted)
            popup.show()
            place(popup)
        self.popup.raise_()
        self.popup.activateWindow()
        self.win.focus_window(int(self.popup.winId()))

    def on_inserted(self, text):
        target, self.target = self.target, 0
        if not target:
            self.icon.showMessage("Saywell", "Copied to the clipboard.", QSystemTrayIcon.Information, 2000)
            return
        log.info("insert %d chars into window %#x", len(text), target)
        self.paster.paste(target)  # runs once the popup has closed

    def on_pasted(self, ok, message):
        if ok:
            log.info("pasted with %s", message)
        else:
            log.warning("paste: %s", message)
            self.icon.showMessage("Saywell", message, QSystemTrayIcon.Warning, 4000)

    # ---- learn / autostart ----------------------------------------------------

    def on_learned(self, ok, message, manual):
        if manual:  # a scheduled run only logs
            self.icon.showMessage("Saywell learn", message[:200],
                                  QSystemTrayIcon.Information if ok else QSystemTrayIcon.Warning, 5000)

    def sync_autostart(self):
        self.autostart_action.blockSignals(True)
        self.autostart_action.setChecked(bool(self.win.run_value(sw.RUN_NAME)))
        self.autostart_action.blockSignals(False)

    def set_autostart(self, on):
        try:
            log.info(sw.autostart(self.win, "enable" if on else "disable"))
        except (OSError, RuntimeError) as e:
            log.error("autostart: %s", e)
            self.warn(f"Could not change the logon setting: {e}")
        self.sync_autostart()


def place(popup):
    """Center the shown popup on the screen with the mouse; if it is larger than the screen,
    keep its top left corner on it."""
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    area = screen.availableGeometry()
    popup.resize(min(popup.width(), area.width() - 40), min(popup.height(), area.height() - 40))
    size = popup.frameGeometry().size()  # the minimum size can win over the resize
    popup.move(area.left() + max(0, (area.width() - size.width()) // 2),
               area.top() + max(0, (area.height() - size.height()) // 2))


def make_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#3584e4"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("white"))
    p.setFont(QFont("Segoe UI", 30, QFont.Bold))
    p.drawText(pix.rect(), Qt.AlignCenter, "S")
    p.end()
    return QIcon(pix)


# ---- single instance ----------------------------------------------------------

def acquire_instance(win, name=sw.INSTANCE_NAME):
    """The mutex handle when this is the first instance of the session, else None."""
    handle, existed = win.create_mutex("Local\\" + name)
    return None if existed else handle


def notify_running(name=sw.INSTANCE_NAME):
    """Ask the running instance to open its popup. True if it got the message."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(2000):
        return False
    sock.write(b"show\n")
    # A pipe may take the bytes at once, and then there is nothing left to wait for.
    ok = sock.waitForBytesWritten(2000) or sock.bytesToWrite() == 0
    sock.disconnectFromServer()
    return ok


def listen(name, on_show, parent):
    """Serve `show` requests from later instances."""
    server = QLocalServer(parent)
    QLocalServer.removeServer(name)
    if not server.listen(name):
        log.warning("cannot listen on %s: %s", name, server.errorString())

    def serve(sock):
        data = bytearray()

        def read():
            data.extend(bytes(sock.readAll()))
            if b"\n" in data:
                sock.readyRead.disconnect(read)
                if data.startswith(b"show"):
                    on_show()
        # The request may be buffered already (no readyRead then) or arrive later.
        sock.readyRead.connect(read)
        sock.disconnected.connect(sock.deleteLater)
        read()

    def on_connection():
        while sock := server.nextPendingConnection():
            serve(sock)

    server.newConnection.connect(on_connection)
    return server


def setup_logging():
    log.setLevel(logging.INFO)
    sw.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(sw.DATA_DIR / "tray.log", maxBytes=512_000, backupCount=1,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    if sys.stderr:  # pythonw has none
        log.addHandler(logging.StreamHandler())
    # Uncaught errors in slots would go to a missing stderr under pythonw.
    sys.excepthook = lambda *exc: log.error("uncaught error", exc_info=exc)


def main(argv=None, win=None):
    ap = argparse.ArgumentParser(description="Saywell tray: global hotkey, paste back, hourly learn.")
    ap.add_argument("--backend", help="path to the saywell backend (default: $SAYWELL_BACKEND or the repo copy)")
    sub = ap.add_subparsers(dest="subcommand")
    auto = sub.add_parser("autostart", help="start the tray at logon (HKCU Run key)")
    auto.add_argument("action", choices=("enable", "disable", "status"))
    auto.add_argument("--command", dest="run_command",
                      help="with enable: the command line to run (default: saywell-tray.exe if installed, "
                           "else this script)")
    args = ap.parse_args(argv)
    if sys.platform != "win32" and win is None:
        sys.exit("saywell_tray: Windows only; on Linux use saywell-popup")
    win = win or win32.Win32()
    if args.subcommand == "autostart":
        try:
            print(sw.autostart(win, args.action, args.run_command))
        except (OSError, RuntimeError) as e:
            sys.exit(f"saywell_tray: {e}")
        return 0

    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)  # closing the popup leaves the tray running
    instance = acquire_instance(win)
    if instance is None:
        return 0 if notify_running() else 1
    setup_logging()
    try:
        settings = sw.load_settings()
    except sw.SettingsError as e:
        log.error("%s", e)
        QMessageBox.critical(None, "Saywell", f"Bad [windows] settings in config.toml:\n\n{e}")
        return 2
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("no system tray; the hotkey still works")
    tray = Tray(win, settings, backend_argv(args.backend))
    server = listen(sw.INSTANCE_NAME, tray.on_hotkey, tray)
    tray.start()
    app.aboutToQuit.connect(tray.stop)
    log.info("started (pid %d, backend %s)", os.getpid(), " ".join(tray.backend))
    code = app.exec()
    server.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
