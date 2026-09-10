import sys
import os
import pty
import fcntl
import termios
import struct
import signal
import json
import time
from collections import deque
from PyQt6 import QtCore, QtGui, QtWidgets
import pyte

PROFILES_DIR = os.path.expanduser("~/.config/server_manager_clean_profiles")
CONFIG_FILE = os.path.expanduser("~/.config/server_manager_clean.json")
AUTOSTART_DESKTOP = os.path.expanduser("~/.config/autostart/server-manager-clean.desktop")

def make_status_icon(running: bool) -> QtGui.QIcon:
    pix = QtGui.QPixmap(14, 14)
    pix.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pix)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    if running:
        p.setBrush(QtGui.QColor("#3fb950"))
        p.setPen(QtGui.QColor("#2ea043"))
    else:
        p.setBrush(QtGui.QColor("#da3633"))
        p.setPen(QtGui.QColor("#b62324"))
    p.drawEllipse(2, 2, 10, 10)
    p.end()
    return QtGui.QIcon(pix)

class TerminalCanvas(QtWidgets.QWidget):
    scroll_changed = QtCore.pyqtSignal(int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cols = 100
        self.rows = 35
        self.history_max = 5000
        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=self.history_max)
        self.stream = pyte.ByteStream(self.screen)
        self.font = QtGui.QFont("Monospace", 10)
        self.font.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.bg_pixmap = None
        self.bg_color = QtGui.QColor("#0d1117")
        self.bg_opacity = 0.82
        self.master_fd = None
        self.scroll_offset = 0
        self.autoscroll = True
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def clear_terminal(self):
        self.screen.reset()
        self.screen.history.top.clear()
        self.screen.history.bottom.clear()
        self.scroll_offset = 0
        self.sync_scroll()
        self.update()

    def set_bg_color(self, hex_code: str):
        if hex_code and QtGui.QColor.isValidColor(hex_code):
            self.bg_color = QtGui.QColor(hex_code)
        else:
            self.bg_color = QtGui.QColor("#0d1117")
        self.update()

    def set_bg_image(self, path: str):
        if path and os.path.exists(path):
            self.bg_pixmap = QtGui.QPixmap(path)
        else:
            self.bg_pixmap = None
        self.update()

    def set_master_fd(self, fd):
        self.master_fd = fd
        self.resize_pty()

    def resize_pty(self):
        if self.master_fd:
            try:
                winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except Exception:
                pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        fm = QtGui.QFontMetrics(self.font)
        cw = max(fm.horizontalAdvance("W"), 1)
        ch = max(fm.height(), 1)
        self.cols = max(10, self.width() // cw)
        self.rows = max(5, self.height() // ch)
        self.screen.resize(self.rows, self.cols)
        self.resize_pty()
        self.sync_scroll()

    def feed_bytes(self, data: bytes):
        prev_top = len(self.screen.history.top)
        self.stream.feed(data)
        if self.autoscroll:
            self.scroll_offset = 0
        else:
            new_top = len(self.screen.history.top)
            self.scroll_offset = min(self.scroll_offset + (new_top - prev_top), new_top)
        self.sync_scroll()
        self.update()

    def sync_scroll(self):
        max_scroll = len(self.screen.history.top)
        val = max_scroll - self.scroll_offset
        self.scroll_changed.emit(0, max_scroll, val)

    def set_scroll_value(self, val):
        max_scroll = len(self.screen.history.top)
        self.scroll_offset = max_scroll - val
        self.update()

    def wheelEvent(self, event: QtGui.QWheelEvent):
        delta = event.angleDelta().y() // 120
        max_scroll = len(self.screen.history.top)
        if delta > 0:
            self.scroll_offset = min(max_scroll, self.scroll_offset + 3)
        elif delta < 0:
            self.scroll_offset = max(0, self.scroll_offset - 3)
        self.sync_scroll()
        self.update()

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        if event.key() == QtCore.Qt.Key.Key_PageUp:
            max_scroll = len(self.screen.history.top)
            self.scroll_offset = min(max_scroll, self.scroll_offset + self.rows)
            self.sync_scroll()
            self.update()
            return
        elif event.key() == QtCore.Qt.Key.Key_PageDown:
            self.scroll_offset = max(0, self.scroll_offset - self.rows)
            self.sync_scroll()
            self.update()
            return

        if self.master_fd is None:
            return
        key_map = {
            QtCore.Qt.Key.Key_Return: b"\r",
            QtCore.Qt.Key.Key_Enter: b"\r",
            QtCore.Qt.Key.Key_Backspace: b"\x7f",
            QtCore.Qt.Key.Key_Tab: b"\t",
            QtCore.Qt.Key.Key_Escape: b"\x1b",
            QtCore.Qt.Key.Key_Up: b"\x1b[A",
            QtCore.Qt.Key.Key_Down: b"\x1b[B",
            QtCore.Qt.Key.Key_Right: b"\x1b[C",
            QtCore.Qt.Key.Key_Left: b"\x1b[D",
        }
        out = b""
        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            k = event.key()
            if QtCore.Qt.Key.Key_A <= k <= QtCore.Qt.Key.Key_Z:
                out = bytes([k - QtCore.Qt.Key.Key_A + 1])
        elif event.key() in key_map:
            out = key_map[event.key()]
        else:
            text = event.text()
            if text:
                out = text.encode("utf-8")
        if out:
            try:
                os.write(self.master_fd, out)
            except OSError:
                pass

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        fm = QtGui.QFontMetrics(self.font)
        cw = fm.horizontalAdvance("W")
        ch = fm.height()

        if self.bg_pixmap and not self.bg_pixmap.isNull():
            scaled = self.bg_pixmap.scaled(self.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding, QtCore.Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(0, 0, scaled)
            dim_color = QtGui.QColor(self.bg_color)
            dim_color.setAlpha(int(255 * self.bg_opacity))
            painter.fillRect(self.rect(), dim_color)
        else:
            painter.fillRect(self.rect(), self.bg_color)

        painter.setFont(self.font)
        color_map = {
            "default": QtGui.QColor("#c9d1d9"),
            "black": QtGui.QColor("#484f58"),
            "red": QtGui.QColor("#ff7b72"),
            "green": QtGui.QColor("#3fb950"),
            "brown": QtGui.QColor("#d29922"),
            "blue": QtGui.QColor("#58a6ff"),
            "magenta": QtGui.QColor("#bc8cff"),
            "cyan": QtGui.QColor("#39c5cf"),
            "white": QtGui.QColor("#f0f6fc"),
        }

        top_history = list(self.screen.history.top)
        top_len = len(top_history)
        cur_buf = self.screen.buffer

        for y in range(self.rows):
            src_idx = top_len - self.scroll_offset + y
            line = None
            if src_idx < top_len:
                line = top_history[src_idx]
            else:
                buf_y = src_idx - top_len
                line = cur_buf.get(buf_y, None)

            if not line:
                continue

            for x in range(self.cols):
                char = line.get(x, None) if isinstance(line, dict) else (line[x] if x < len(line) else None)
                if not char or char.data == " ":
                    continue
                fg = color_map.get(char.fg, color_map["default"])
                painter.setPen(fg)
                painter.drawText(x * cw, (y + 1) * ch - fm.descent(), char.data)

        if self.hasFocus() and self.scroll_offset == 0:
            cur_x = self.screen.cursor.x * cw
            cur_y = self.screen.cursor.y * ch
            painter.fillRect(QtCore.QRect(cur_x, cur_y, cw, ch), QtGui.QColor(255, 255, 255, 120))

class ServerConfigDialog(QtWidgets.QDialog):
    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки сервера")
        self.resize(520, 460)
        layout = QtWidgets.QFormLayout(self)

        self.name_edit = QtWidgets.QLineEdit(data.get("name", "Новый сервер"))
        self.cmd_edit = QtWidgets.QLineEdit(data.get("cmd", ""))
        self.args_edit = QtWidgets.QLineEdit(data.get("args", ""))
        self.cwd_edit = QtWidgets.QLineEdit(data.get("cwd", ""))
        self.env_edit = QtWidgets.QPlainTextEdit(data.get("env", ""))

        self.selected_color = data.get("bg_color", "#0d1117")
        self.color_preview = QtWidgets.QLabel()
        self.color_preview.setFixedSize(36, 24)
        self.update_color_preview()

        color_btn = QtWidgets.QPushButton("Выбрать цвет...")
        color_btn.clicked.connect(self.select_color)
        reset_color_btn = QtWidgets.QPushButton("Сбросить")
        reset_color_btn.clicked.connect(self.reset_color)

        color_layout = QtWidgets.QHBoxLayout()
        color_layout.addWidget(self.color_preview)
        color_layout.addWidget(color_btn)
        color_layout.addWidget(reset_color_btn)
        color_layout.addStretch()

        self.bg_edit = QtWidgets.QLineEdit(data.get("bg_image", ""))
        bg_btn = QtWidgets.QPushButton("Выбрать...")
        bg_btn.clicked.connect(self.select_bg)
        bg_reset_btn = QtWidgets.QPushButton("Сбросить")
        bg_reset_btn.clicked.connect(lambda: self.bg_edit.setText(""))

        bg_layout = QtWidgets.QHBoxLayout()
        bg_layout.addWidget(self.bg_edit)
        bg_layout.addWidget(bg_btn)
        bg_layout.addWidget(bg_reset_btn)

        layout.addRow("Имя вкладки:", self.name_edit)
        layout.addRow("Команда / бинарник:", self.cmd_edit)
        layout.addRow("Аргументы:", self.args_edit)
        layout.addRow("Рабочая папка:", self.cwd_edit)
        layout.addRow("ENV (KEY=VAL):", self.env_edit)
        layout.addRow("Цвет фона терминала:", color_layout)
        layout.addRow("Фоновая картинка:", bg_layout)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def update_color_preview(self):
        self.color_preview.setStyleSheet(f"background-color: {self.selected_color}; border: 1px solid #ffffff; border-radius: 3px;")

    def select_color(self):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.selected_color), self, "Выбор цвета фона")
        if c.isValid():
            self.selected_color = c.name()
            self.update_color_preview()

    def reset_color(self):
        self.selected_color = "#0d1117"
        self.update_color_preview()

    def select_bg(self):
        file, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Выбрать фоновую картинку", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if file:
            self.bg_edit.setText(file)

    def get_data(self):
        return {
            "name": self.name_edit.text().strip(),
            "cmd": self.cmd_edit.text().strip(),
            "args": self.args_edit.text().strip(),
            "cwd": self.cwd_edit.text().strip(),
            "env": self.env_edit.toPlainText().strip(),
            "bg_color": self.selected_color,
            "bg_image": self.bg_edit.text().strip(),
        }

class IconsConfigDialog(QtWidgets.QDialog):
    def __init__(self, icons_data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройка иконок приложения")
        self.resize(520, 220)
        self.icons = dict(icons_data)

        layout = QtWidgets.QFormLayout(self)

        self.preview_window = QtWidgets.QLabel()
        self.preview_window.setFixedSize(28, 28)
        self.preview_tray = QtWidgets.QLabel()
        self.preview_tray.setFixedSize(28, 28)
        self.preview_panel = QtWidgets.QLabel()
        self.preview_panel.setFixedSize(28, 28)

        layout.addRow("Иконка окна:", self._create_row("window", self.preview_window))
        layout.addRow("Иконка трея:", self._create_row("tray", self.preview_tray))
        layout.addRow("Иконка панели KDE:", self._create_row("panel", self.preview_panel))

        self.update_previews()

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _create_row(self, key, preview_label):
        box = QtWidgets.QHBoxLayout()
        edit = QtWidgets.QLineEdit(self.icons.get(key, ""))
        edit.setReadOnly(True)
        setattr(self, f"edit_{key}", edit)

        btn_pick = QtWidgets.QPushButton("Выбрать...")
        btn_pick.clicked.connect(lambda: self.pick_file(key, edit, preview_label))
        btn_reset = QtWidgets.QPushButton("Сбросить")
        btn_reset.clicked.connect(lambda: self.reset_file(key, edit, preview_label))

        box.addWidget(preview_label)
        box.addWidget(edit)
        box.addWidget(btn_pick)
        box.addWidget(btn_reset)
        widget = QtWidgets.QWidget()
        widget.setLayout(box)
        return widget

    def pick_file(self, key, edit, preview):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Выбрать иконку", "", "Images (*.png *.ico *.svg *.xpm)")
        if f:
            self.icons[key] = f
            edit.setText(f)
            self.set_preview_pixmap(preview, f)

    def reset_file(self, key, edit, preview):
        self.icons[key] = ""
        edit.setText("")
        preview.setPixmap(QtGui.QPixmap())

    def set_preview_pixmap(self, label, path):
        if path and os.path.exists(path):
            pix = QtGui.QPixmap(path).scaled(24, 24, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
            label.setPixmap(pix)
        else:
            label.setPixmap(QtGui.QPixmap())

    def update_previews(self):
        self.set_preview_pixmap(self.preview_window, self.icons.get("window", ""))
        self.set_preview_pixmap(self.preview_tray, self.icons.get("tray", ""))
        self.set_preview_pixmap(self.preview_panel, self.icons.get("panel", ""))

    def get_data(self):
        return self.icons

class ServerTab(QtWidgets.QWidget):
    status_changed = QtCore.pyqtSignal()
    notify_message = QtCore.pyqtSignal(str, str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.pid = None
        self.master_fd = None
        self.notifier = None

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.terminal = TerminalCanvas(self)
        self.apply_config_styles()

        self.scrollbar = QtWidgets.QScrollBar(QtCore.Qt.Orientation.Vertical, self)
        self.scrollbar.setRange(0, 0)
        self.scrollbar.valueChanged.connect(self.terminal.set_scroll_value)
        self.terminal.scroll_changed.connect(self.on_scroll_changed)

        layout.addWidget(self.terminal)
        layout.addWidget(self.scrollbar)

    def apply_config_styles(self):
        self.terminal.set_bg_color(self.config.get("bg_color", "#0d1117"))
        self.terminal.set_bg_image(self.config.get("bg_image", ""))

    def on_scroll_changed(self, min_val, max_val, cur_val):
        self.scrollbar.blockSignals(True)
        self.scrollbar.setRange(min_val, max_val)
        self.scrollbar.setValue(cur_val)
        self.scrollbar.blockSignals(False)

    def set_autoscroll(self, enabled: bool):
        self.terminal.autoscroll = enabled

    def clear_log(self):
        self.terminal.clear_terminal()

    def is_running(self):
        return self.pid is not None

    def start_server(self):
        if self.is_running():
            return
        cmd = self.config.get("cmd", "")
        name = self.config.get("name", "Сервер")
        if not cmd:
            self.terminal.feed_bytes(b"[ERROR] Command not specified\r\n")
            return

        master, slave = pty.openpty()
        env = os.environ.copy()
        for line in self.config.get("env", "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

        args = [cmd] + self.config.get("args", "").split()
        cwd = self.config.get("cwd", "")
        if not cwd or not os.path.exists(cwd):
            cwd = os.path.expanduser("~")

        pid = os.fork()
        if pid == 0:
            os.close(master)
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            if slave > 2:
                os.close(slave)
            os.chdir(cwd)
            try:
                os.execvpe(cmd, args, env)
            except Exception:
                pass
            os._exit(1)
        else:
            os.close(slave)
            self.pid = pid
            self.master_fd = master
            self.terminal.set_master_fd(master)
            flags = fcntl.fcntl(master, fcntl.F_GETFL)
            fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            self.notifier = QtCore.QSocketNotifier(master, QtCore.QSocketNotifier.Type.Read, self)
            self.notifier.activated.connect(self.handle_stdout)
            self.status_changed.emit()
            self.notify_message.emit("Сервер запущен", f"{name} успешно запущен")

    def handle_stdout(self):
        try:
            data = os.read(self.master_fd, 4096)
            if data:
                self.terminal.feed_bytes(data)
            else:
                self.cleanup_child(notify=True)
        except OSError:
            self.cleanup_child(notify=True)

    def stop_server(self, timeout=2.5, notify=True):
        if not self.is_running():
            return
        name = self.config.get("name", "Сервер")
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            self.cleanup_child(notify=notify)
            return

        start_t = time.time()
        while time.time() - start_t < timeout:
            res, _ = os.waitpid(self.pid, os.WNOHANG)
            if res != 0:
                self.pid = None
                self.cleanup_child(notify=notify)
                return
            time.sleep(0.05)

        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        self.cleanup_child(notify=notify)

    def restart_server(self):
        self.stop_server(notify=False)
        QtCore.QTimer.singleShot(400, self.start_server)

    def cleanup_child(self, notify=False):
        name = self.config.get("name", "Сервер")
        was_running = self.pid is not None
        if self.notifier:
            self.notifier.setEnabled(False)
            self.notifier = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.pid is not None:
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass
            self.pid = None
        self.status_changed.emit()
        if notify and was_running:
            self.notify_message.emit("Сервер остановлен", f"{name} остановлен")

    def open_settings(self, rename_callback=None):
        dlg = ServerConfigDialog(self.config, self)
        if dlg.exec():
            self.config = dlg.get_data()
            self.apply_config_styles()
            if rename_callback:
                rename_callback(self.config.get("name", "Сервер"))
            self.status_changed.emit()

class FlowLayout(QtWidgets.QLayout):
    def __init__(self, parent=None, margin=2, h_spacing=4, v_spacing=4):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def horizontalSpacing(self):
        return self._h_spacing

    def verticalSpacing(self):
        return self._v_spacing

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QtCore.QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QtCore.QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        effective_rect = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective_rect.x()
        y = effective_rect.y()
        line_height = 0

        for item in self._items:
            space_x = self.horizontalSpacing()
            space_y = self.verticalSpacing()
            hint = item.sizeHint()
            next_x = x + hint.width() + space_x

            if next_x - space_x > effective_rect.right() and line_height > 0:
                x = effective_rect.x()
                y = y + line_height + space_y
                next_x = x + hint.width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))

            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()

class DraggableTabButton(QtWidgets.QPushButton):
    def __init__(self, text, index, parent=None):
        super().__init__(text, parent)
        self.tab_index = index
        self.setCheckable(True)
        self.setAutoExclusive(False)
        self.drag_start_pos = None
        self.setAcceptDrops(True)
        self.setIcon(make_status_icon(False))
        self.setIconSize(QtCore.QSize(14, 14))
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setStyleSheet("""
            QPushButton {
                background-color: #1e2228;
                color: #8b949e;
                border: 1px solid #30363d;
                border-radius: 4px;
                padding: 0 14px 0 8px;
                font-size: 12px;
                text-align: left;
            }
            QPushButton:hover:!checked {
                background-color: #282e36;
                color: #c9d1d9;
                border-color: #484f58;
            }
            QPushButton:checked {
                background-color: #1f6feb;
                color: #ffffff;
                font-weight: bold;
                border: 2px solid #58a6ff;
                padding: 0 14px 0 7px;
            }
        """)

    def sizeHint(self):
        bold_font = QtGui.QFont(self.font())
        bold_font.setBold(True)
        bold_font.setPointSize(9)
        fm = QtGui.QFontMetrics(bold_font)
        text_w = fm.horizontalAdvance(self.text())
        total_w = text_w + 14 + 10 + 26
        return QtCore.QSize(total_w, 28)

    def minimumSizeHint(self):
        return self.sizeHint()

    def set_running_status(self, running: bool):
        self.setIcon(make_status_icon(running))

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.drag_start_pos = event.position().toPoint()
            self.window().tabs.set_current_index(self.tab_index)
            return
        elif event.button() == QtCore.Qt.MouseButton.MiddleButton:
            self.window().tabs.tabCloseRequested.emit(self.tab_index)
            return
        elif event.button() == QtCore.Qt.MouseButton.RightButton:
            menu = QtWidgets.QMenu(self)
            act_close = menu.addAction("Закрыть вкладку")
            chosen = menu.exec(event.globalPosition().toPoint())
            if chosen == act_close:
                self.window().tabs.tabCloseRequested.emit(self.tab_index)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        if not (event.buttons() & QtCore.Qt.MouseButton.LeftButton) or not self.drag_start_pos:
            return
        if (event.position().toPoint() - self.drag_start_pos).manhattanLength() < QtWidgets.QApplication.startDragDistance():
            return
        drag = QtGui.QDrag(self)
        mime = QtCore.QMimeData()
        mime.setText(str(self.tab_index))
        drag.setMimeData(mime)
        drag.exec(QtCore.Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event: QtGui.QDropEvent):
        try:
            src_idx = int(event.mimeData().text())
            dst_idx = self.tab_index
            if src_idx != dst_idx:
                self.window().tabs.move_tab(src_idx, dst_idx)
            event.acceptProposedAction()
        except ValueError:
            pass

class MultiRowTabWidget(QtWidgets.QWidget):
    currentChanged = QtCore.pyqtSignal(int)
    tabCloseRequested = QtCore.pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.btn_container = QtWidgets.QWidget(self)
        self.flow_layout = FlowLayout(self.btn_container, margin=3, h_spacing=4, v_spacing=4)
        self.btn_container.setLayout(self.flow_layout)

        self.stack = QtWidgets.QStackedWidget(self)
        layout.addWidget(self.btn_container)
        layout.addWidget(self.stack)

        self.buttons = []
        self._current_index = -1

    def count(self):
        return self.stack.count()

    def widget(self, idx):
        return self.stack.widget(idx)

    def currentWidget(self):
        return self.stack.currentWidget()

    def currentIndex(self):
        return self._current_index

    def addTab(self, widget, text):
        idx = self.stack.addWidget(widget)
        btn = DraggableTabButton(text, idx, self.btn_container)
        self.flow_layout.addWidget(btn)
        self.buttons.append(btn)
        self.reindex_buttons()
        self.set_current_index(idx)
        return idx

    def update_tab_status(self, idx, is_running):
        if 0 <= idx < len(self.buttons):
            self.buttons[idx].set_running_status(is_running)

    def set_current_index(self, idx):
        if 0 <= idx < self.count():
            self._current_index = idx
            self.stack.setCurrentIndex(idx)
            for i, b in enumerate(self.buttons):
                b.blockSignals(True)
                b.setChecked(i == idx)
                b.blockSignals(False)
            self.currentChanged.emit(idx)
        elif self.count() == 0:
            self._current_index = -1
            self.currentChanged.emit(-1)

    def setTabText(self, idx, text):
        if 0 <= idx < len(self.buttons):
            self.buttons[idx].setText(text)
            self.buttons[idx].updateGeometry()
            self.flow_layout.invalidate()
            self.btn_container.adjustSize()

    def removeTab(self, idx):
        if 0 <= idx < self.count():
            w = self.stack.widget(idx)
            self.stack.removeWidget(w)
            btn = self.buttons.pop(idx)
            self.flow_layout.removeWidget(btn)
            btn.deleteLater()
            self.reindex_buttons()
            new_idx = min(idx, self.count() - 1)
            self.set_current_index(new_idx)

    def reindex_buttons(self):
        for i, b in enumerate(self.buttons):
            b.tab_index = i

    def move_tab(self, src_idx, dst_idx):
        if src_idx == dst_idx or src_idx >= self.count() or dst_idx >= self.count():
            return
        w = self.stack.widget(src_idx)
        b = self.buttons.pop(src_idx)
        self.stack.removeWidget(w)
        self.flow_layout.removeWidget(b)

        self.stack.insertWidget(dst_idx, w)
        self.buttons.insert(dst_idx, b)

        for item_btn in self.buttons:
            self.flow_layout.removeWidget(item_btn)
            self.flow_layout.addWidget(item_btn)

        self.reindex_buttons()
        self.set_current_index(dst_idx)

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Менеджер серверов (Clean)")
        self.resize(1150, 720)

        self.close_action = "close"
        self.autoscroll_enabled = True
        self.show_button_text = True
        self.force_exit_flag = False
        self.autostart_enabled = False
        self.start_minimized = False
        self.notifications_enabled = True

        self.custom_icons = {
            "window": "",
            "tray": "",
            "panel": ""
        }

        self.tabs = MultiRowTabWidget(self)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self.update_control_state)
        self.setCentralWidget(self.tabs)

        self.create_menu()
        self.create_tray()
        self.load_state()

    def show_about_dialog(self):
        title = "О программе Server Manager"
        text = """<h3>Server Manager v1.0</h3>
        <p>Менеджер локальных серверов и LLM-инференсов для Arch Linux.</p>
        <p><b>Возможности:</b></p>
        <ul>
            <li>Встроенный ANSI TTY-терминал с поддержкой цветов;</li>
            <li>Плавное завершение процессов (SIGTERM/SIGKILL);</li>
            <li>Управление профилями и системный трей;</li>
            <li>Автозапуск и фоновый режим.</li>
        </ul>
        <p>Лицензия: <b>GPLv3</b></p>"""
        QtWidgets.QMessageBox.about(self, title, text)


    def create_menu(self):
        bar = self.menuBar()

        # 1. Серверы
        m_srv = bar.addMenu("Серверы")
        act_add = QtGui.QAction("+ Добавить сервер", self)
        act_add.triggered.connect(self.add_new_server_action)
        m_srv.addAction(act_add)

        # 2. Профили
        m_prof = bar.addMenu("Профили")
        act_save_prof = QtGui.QAction("Сохранить профиль...", self)
        act_save_prof.triggered.connect(self.profile_save)
        m_prof.addAction(act_save_prof)

        act_load_prof = QtGui.QAction("Загрузить профиль...", self)
        act_load_prof.triggered.connect(self.profile_load)
        m_prof.addAction(act_load_prof)

        act_del_prof = QtGui.QAction("Удалить профиль...", self)
        act_del_prof.triggered.connect(self.profile_delete)
        m_prof.addAction(act_del_prof)

        m_prof.addSeparator()
        act_close_all = QtGui.QAction("Закрыть все вкладки", self)
        act_close_all.triggered.connect(self.close_all_tabs)
        m_prof.addAction(act_close_all)

        # 3. Интерфейс
        m_view = bar.addMenu("Интерфейс")
        act_icons = QtGui.QAction("Настройка иконок...", self)
        act_icons.triggered.connect(self.open_icons_dialog)
        m_view.addAction(act_icons)

        # 4. О программе (ставим сразу после "Интерфейс")
        m_help = bar.addMenu("Справка")
        act_about = QtGui.QAction("О программе...", self)
        act_about.triggered.connect(self.show_about_dialog)
        m_help.addAction(act_about)

        self.act_btn_text = QtGui.QAction("Текст на кнопках управления", self, checkable=True)
        self.act_btn_text.setChecked(True)
        self.act_btn_text.toggled.connect(self.toggle_buttons_text)
        m_view.addAction(self.act_btn_text)

        self.act_autoscroll = QtGui.QAction("Автоскролл терминала", self, checkable=True)
        self.act_autoscroll.setChecked(True)
        self.act_autoscroll.toggled.connect(self.toggle_autoscroll)
        m_view.addAction(self.act_autoscroll)

        self.act_notifications = QtGui.QAction("Включить уведомления", self, checkable=True)
        self.act_notifications.setChecked(True)
        self.act_notifications.toggled.connect(lambda s: setattr(self, "notifications_enabled", s))
        m_view.addAction(self.act_notifications)

        self.act_toggle_tray = QtGui.QAction("Включить системный трей", self, checkable=True)
        self.act_toggle_tray.setChecked(True)
        self.act_toggle_tray.toggled.connect(self.toggle_tray_visible)
        m_view.addAction(self.act_toggle_tray)

        self.act_autostart = QtGui.QAction("Автозапуск с системой", self, checkable=True)
        self.act_autostart.toggled.connect(self.toggle_autostart)
        m_view.addAction(self.act_autostart)

        self.act_start_min = QtGui.QAction("Запуск в свёрнутом виде (на панель)", self, checkable=True)
        self.act_start_min.toggled.connect(lambda s: setattr(self, "start_minimized", s))
        m_view.addAction(self.act_start_min)

        m_close = m_view.addMenu("Поведение кнопки [X]")
        self.act_close_quit = QtGui.QAction("Закрыть приложение", self, checkable=True)
        self.act_close_min = QtGui.QAction("Сворачивать на панель", self, checkable=True)
        self.act_close_tray = QtGui.QAction("Сворачивать в трей", self, checkable=True)

        grp = QtGui.QActionGroup(self)
        for a in (self.act_close_quit, self.act_close_min, self.act_close_tray):
            grp.addAction(a)
            m_close.addAction(a)

        self.act_close_quit.setChecked(True)
        self.act_close_quit.triggered.connect(lambda: self.set_close_mode("close"))
        self.act_close_min.triggered.connect(lambda: self.set_close_mode("minimize"))
        self.act_close_tray.triggered.connect(lambda: self.set_close_mode("tray"))

        # Панель кнопок в правом углу строки меню
        self.nav_widget = QtWidgets.QWidget(bar)
        self.nav_layout = QtWidgets.QHBoxLayout(self.nav_widget)
        self.nav_layout.setContentsMargins(0, 0, 6, 0)
        self.nav_layout.setSpacing(5)
        self.nav_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self.lbl_status = QtWidgets.QLabel("Остановлен")
        self.lbl_status.setStyleSheet("color: #8b949e; font-weight: bold; margin-right: 8px;")

        self.btn_start = QtWidgets.QPushButton()
        self.btn_stop = QtWidgets.QPushButton()
        self.btn_restart = QtWidgets.QPushButton()
        self.btn_stop_all = QtWidgets.QPushButton()
        self.btn_clear = QtWidgets.QPushButton()
        self.btn_cfg = QtWidgets.QPushButton()

        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_stop_all, self.btn_clear, self.btn_cfg):
            self.nav_layout.addWidget(b)

        nav_layout = self.nav_layout
        nav_layout.addWidget(self.lbl_status)
        bar.setCornerWidget(self.nav_widget, QtCore.Qt.Corner.TopRightCorner)

        self.btn_start.clicked.connect(self.current_start)
        self.btn_stop.clicked.connect(self.current_stop)
        self.btn_restart.clicked.connect(self.current_restart)
        self.btn_stop_all.clicked.connect(self.stop_all_servers)
        self.btn_clear.clicked.connect(self.current_clear)
        self.btn_cfg.clicked.connect(self.current_cfg)

        self.update_buttons_ui()

    def update_buttons_ui(self):
        btns = (self.btn_start, self.btn_stop, self.btn_restart, self.btn_stop_all, self.btn_clear, self.btn_cfg)
        if self.show_button_text:
            self.btn_start.setText("▶ Старт")
            self.btn_stop.setText("⏹ Стоп")
            self.btn_restart.setText("🔄 Перезапуск")
            self.btn_stop_all.setText("🛑 Стоп все")
            self.btn_clear.setText("🧹 Очистить")
            self.btn_cfg.setText("⚙ Настройки")
            style = """
                QPushButton {
                    background-color: #21262d;
                    color: #c9d1d9;
                    border: 1px solid #30363d;
                    border-radius: 4px;
                    padding: 0 8px;
                    font-size: 12px;
                    height: 24px;
                }
                QPushButton:hover {
                    background-color: #30363d;
                    color: #ffffff;
                }
                QPushButton:pressed {
                    background-color: #161b22;
                }
            """
            for b in btns:
                b.setMinimumSize(QtCore.QSize(0, 24))
                b.setMaximumSize(QtCore.QSize(16777215, 24))
                b.setStyleSheet(style)
        else:
            self.btn_start.setText("▶")
            self.btn_stop.setText("⏹")
            self.btn_restart.setText("🔄")
            self.btn_stop_all.setText("🛑")
            self.btn_clear.setText("🧹")
            self.btn_cfg.setText("⚙")
            style = """
                QPushButton {
                    background-color: #21262d;
                    color: #c9d1d9;
                    border: 1px solid #30363d;
                    border-radius: 4px;
                    font-size: 13px;
                    text-align: center;
                }
                QPushButton:hover {
                    background-color: #30363d;
                    color: #ffffff;
                }
                QPushButton:pressed {
                    background-color: #161b22;
                }
            """
            for b in btns:
                b.setFixedSize(26, 26)
                b.setStyleSheet(style)

        self.btn_start.setToolTip("Запустить сервер (▶)")
        self.btn_stop.setToolTip("Остановить сервер (⏹)")
        self.btn_restart.setToolTip("Перезапустить сервер (🔄)")
        self.btn_stop_all.setToolTip("Остановить все запущенные серверы (🛑)")
        self.btn_clear.setToolTip("Очистить лог терминала (🧹)")
        self.btn_cfg.setToolTip("Настройки сервера (⚙)")

        self.nav_widget.adjustSize()
        self.menuBar().adjustSize()

    def toggle_buttons_text(self, state):
        self.show_button_text = state
        self.update_buttons_ui()

    def set_close_mode(self, mode):
        self.close_action = mode

    def toggle_autoscroll(self, state):
        self.autoscroll_enabled = state
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ServerTab):
                w.set_autoscroll(state)

    def toggle_tray_visible(self, state):
        if hasattr(self, "tray"):
            self.tray.setVisible(state)

    def toggle_autostart(self, state):
        self.autostart_enabled = state
        os.makedirs(os.path.dirname(AUTOSTART_DESKTOP), exist_ok=True)
        if state:
            py_path = sys.executable
            script_path = os.path.abspath(sys.argv[0])
            content = f"""[Desktop Entry]
Type=Application
Version=1.0
Name=Server Manager
Comment=Qt Server Manager Autostart
Exec={py_path} {script_path}
Terminal=false
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""
            try:
                with open(AUTOSTART_DESKTOP, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                self.show_notification("Ошибка автозапуска", str(e))
        else:
            if os.path.exists(AUTOSTART_DESKTOP):
                try:
                    os.remove(AUTOSTART_DESKTOP)
                except OSError:
                    pass

    def open_icons_dialog(self):
        dlg = IconsConfigDialog(self.custom_icons, self)
        if dlg.exec():
            self.custom_icons = dlg.get_data()
            self.apply_icons()

    def apply_icons(self):
        w_icon = self.custom_icons.get("window", "")
        if w_icon and os.path.exists(w_icon):
            self.setWindowIcon(QtGui.QIcon(w_icon))
        else:
            p_icon = self.custom_icons.get("panel", "")
            if p_icon and os.path.exists(p_icon):
                self.setWindowIcon(QtGui.QIcon(p_icon))
            else:
                self.setWindowIcon(QtGui.QIcon())

        t_icon = self.custom_icons.get("tray", "")
        if t_icon and os.path.exists(t_icon):
            self.tray.setIcon(QtGui.QIcon(t_icon))
        else:
            self.tray.setIcon(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon))

    def show_notification(self, title, msg):
        if self.notifications_enabled and hasattr(self, "tray") and self.tray.isVisible():
            self.tray.showMessage(title, msg, QtWidgets.QSystemTrayIcon.MessageIcon.Information, 2500)

    def current_tab(self):
        w = self.tabs.currentWidget()
        return w if isinstance(w, ServerTab) else None

    def current_start(self):
        t = self.current_tab()
        if t: t.start_server()

    def current_stop(self):
        t = self.current_tab()
        if t: t.stop_server()

    def current_restart(self):
        t = self.current_tab()
        if t: t.restart_server()

    def stop_all_servers(self):
        stopped_count = 0
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ServerTab) and w.is_running():
                w.stop_server(timeout=1.5, notify=False)
                stopped_count += 1
        self.update_tray_tooltip()
        if stopped_count > 0:
            self.show_notification("Остановка серверов", f"Остановлено серверов: {stopped_count}")

    def current_clear(self):
        t = self.current_tab()
        if t: t.clear_log()

    def current_cfg(self):
        t = self.current_tab()
        if t:
            idx = self.tabs.currentIndex()
            t.open_settings(rename_callback=lambda new_name: self.tabs.setTabText(idx, new_name))

    def update_control_state(self):
        if not hasattr(self, "lbl_status"):
            return
        t = self.current_tab()
        if not t:
            self.lbl_status.setText("Нет вкладок")
            self.lbl_status.setStyleSheet("color: #8b949e; margin-right: 8px;")
            return
        if t.is_running():
            self.lbl_status.setText("Работает")
            self.lbl_status.setStyleSheet("color: #3fb950; font-weight: bold; margin-right: 8px;")
        else:
            self.lbl_status.setText("Остановлен")
            self.lbl_status.setStyleSheet("color: #8b949e; font-weight: bold; margin-right: 8px;")

    def create_tray(self):
        self.tray = QtWidgets.QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon))
        menu = QtWidgets.QMenu()
        act_restore = menu.addAction("Развернуть")
        act_restore.triggered.connect(self.showNormal)
        act_stop_all = menu.addAction("Остановить все сервера")
        act_stop_all.triggered.connect(self.stop_all_servers)
        menu.addSeparator()
        act_exit = menu.addAction("Выход")
        act_exit.triggered.connect(self.force_exit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()
        self.update_tray_tooltip()

    def on_tray_activated(self, reason):
        if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.showNormal()
                self.activateWindow()

    def force_exit(self):
        self.force_exit_flag = True
        self.close()

    def update_tray_tooltip(self):
        running = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ServerTab):
                is_r = w.is_running()
                self.tabs.update_tab_status(i, is_r)
                if is_r:
                    running.append(w.config.get("name", "Сервер"))
        if running:
            self.tray.setToolTip("Активные серверы:\n• " + "\n• ".join(running))
        else:
            self.tray.setToolTip("Все серверы остановлены")
        self.update_control_state()

    def add_tab(self, cfg):
        tab = ServerTab(cfg, self.tabs)
        tab.set_autoscroll(self.autoscroll_enabled)
        tab.status_changed.connect(self.update_tray_tooltip)
        tab.notify_message.connect(self.show_notification)
        idx = self.tabs.addTab(tab, cfg.get("name", "Сервер"))
        self.tabs.update_tab_status(idx, tab.is_running())

    def add_new_server_action(self):
        dlg = ServerConfigDialog({}, self)
        if dlg.exec():
            self.add_tab(dlg.get_data())

    def close_tab(self, idx):
        w = self.tabs.widget(idx)
        if isinstance(w, ServerTab):
            w.stop_server(notify=False)
        self.tabs.removeTab(idx)
        self.update_tray_tooltip()

    def close_all_tabs(self):
        while self.tabs.count() > 0:
            self.close_tab(0)

    def profile_save(self):
        os.makedirs(PROFILES_DIR, exist_ok=True)
        name, ok = QtWidgets.QInputDialog.getText(self, "Сохранить профиль", "Имя нового профиля:")
        if ok and name.strip():
            filepath = os.path.join(PROFILES_DIR, f"{name.strip()}.json")
            out = []
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if isinstance(w, ServerTab):
                    out.append(w.config)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2, ensure_ascii=False)
            self.show_notification("Профиль сохранен", f"Профиль '{name}' успешно сохранен")

    def profile_load(self):
        os.makedirs(PROFILES_DIR, exist_ok=True)
        files = [f[:-5] for f in os.listdir(PROFILES_DIR) if f.endswith(".json")]
        if not files:
            QtWidgets.QMessageBox.warning(self, "Загрузка профиля", "Нет доступных сохраненных профилей.")
            return
        name, ok = QtWidgets.QInputDialog.getItem(self, "Загрузить профиль", "Выберите профиль:", files, 0, False)
        if ok and name:
            filepath = os.path.join(PROFILES_DIR, f"{name}.json")
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    configs = json.load(f)
                    self.close_all_tabs()
                    for cfg in configs:
                        self.add_tab(cfg)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить: {e}")

    def profile_delete(self):
        os.makedirs(PROFILES_DIR, exist_ok=True)
        files = [f[:-5] for f in os.listdir(PROFILES_DIR) if f.endswith(".json")]
        if not files:
            QtWidgets.QMessageBox.warning(self, "Удаление", "Нет профилей для удаления.")
            return
        name, ok = QtWidgets.QInputDialog.getItem(self, "Удалить профиль", "Выберите профиль:", files, 0, False)
        if ok and name:
            filepath = os.path.join(PROFILES_DIR, f"{name}.json")
            try:
                os.remove(filepath)
                self.show_notification("Удалено", f"Профиль '{name}' удален")
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Ошибка", f"Не удалось удалить: {e}")

    def load_state(self):
        data = None
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                    data = []
                    if isinstance(raw, dict):
                        data = raw.get("servers", [])
                        self.close_action = raw.get("close_action", "close")
                        self.show_button_text = raw.get("show_button_text", True)
                        self.autostart_enabled = raw.get("autostart", False)
                        self.start_minimized = raw.get("start_minimized", False)
                        self.notifications_enabled = raw.get("notifications", True)
                        self.custom_icons = raw.get("custom_icons", self.custom_icons)

                        self.act_btn_text.setChecked(self.show_button_text)
                        self.act_autostart.setChecked(self.autostart_enabled)
                        self.act_start_min.setChecked(self.start_minimized)
                        self.act_notifications.setChecked(self.notifications_enabled)
                        self.update_buttons_ui()
                        self.apply_icons()

                        if self.close_action == "minimize":
                            self.act_close_min.setChecked(True)
                        elif self.close_action == "tray":
                            self.act_close_tray.setChecked(True)
                        else:
                            self.act_close_quit.setChecked(True)
                    elif isinstance(raw, list):
                        data = raw
                    for cfg in data:
                        self.add_tab(cfg)
            except Exception:
                pass

    def save_state(self):
        out = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ServerTab):
                out.append(w.config)
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "close_action": self.close_action,
                "show_button_text": self.show_button_text,
                "autostart": self.autostart_enabled,
                "start_minimized": self.start_minimized,
                "notifications": self.notifications_enabled,
                "custom_icons": self.custom_icons,
                "servers": out
            }, f, indent=2, ensure_ascii=False)

    def closeEvent(self, event):
        if not self.force_exit_flag:
            if self.close_action == "minimize":
                event.ignore()
                self.showMinimized()
                return
            elif self.close_action == "tray":
                event.ignore()
                self.hide()
                return

        self.save_state()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ServerTab):
                w.stop_server(timeout=1.5, notify=False)
        if hasattr(self, "tray"):
            self.tray.hide()
        event.accept()

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setDesktopFileName("server-manager-clean")
    app.setStyle("Fusion")
    win = MainWindow()
    if win.start_minimized:
        win.showMinimized()
    else:
        win.show()
    sys.exit(app.exec())
