"""Native Qt desktop views for the existing connector CLI."""
from pathlib import Path
import time

from PySide6.QtCore import QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QMainWindow,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .session import ROOT, Session, read_json, write_json
from proto.dns_config import DNS_PRESETS, normalize_dns

INK = '#193438'
TEAL = '#087f79'

STYLE = """
QMainWindow, QWidget#canvas, QWidget#overview { background: #f5f7f6; }
QWidget { color: #193438; font-family: 'Segoe UI'; font-size: 14px; }
QFrame#sidebar { background: #173437; }
QLabel { background: transparent; }
QLabel#brand { color: #ffffff; font-size: 23px; font-weight: 700; }
QLabel#sideCaption { color: #9fb9b9; font-size: 12px; }
QLabel#sideNote { color: #c4d6d5; font-size: 12px; }
QLabel#eyebrow { color: #6b8586; font-size: 11px; font-weight: 700; letter-spacing: 2px; }
QLabel#title { font-size: 29px; font-weight: 700; letter-spacing: -1px; }
QLabel#subtitle { color: #748585; font-size: 13px; }
QLabel#heroTitle { font-size: 32px; font-weight: 700; letter-spacing: -1px; }
QLabel#sectionTitle { font-size: 18px; font-weight: 650; }
QLabel#muted { color: #738686; font-size: 12px; }
QLabel#consoleName { font-size: 15px; font-weight: 650; }
QLabel#mono { color: #738686; font-family: 'Cascadia Mono', 'Consolas'; font-size: 11px; }
QLabel#metric { font-size: 23px; font-weight: 650; }
QLabel#message { background: #fff1db; color: #815522; border-radius: 10px; padding: 12px; }
QFrame#card { background: #ffffff; border: 1px solid #e0e8e5; border-radius: 16px; }
QFrame#hero { background: #e5f1ec; border: 1px solid #d7e9e0; border-radius: 18px; }
QFrame#row { background: #f7f9f8; border: 1px solid #edf0ee; border-radius: 11px; }
QFrame#line { background: #e6ece9; max-height: 1px; }
QPushButton { border: 1px solid #dce5e1; background: #ffffff; border-radius: 8px;
              padding: 9px 15px; font-weight: 600; }
QPushButton:hover { background: #edf6f1; border-color: #afcdc2; }
QPushButton:pressed { background: #d8e9e1; }
QPushButton:focus { border: 2px solid #3da9a0; }
QPushButton:disabled { color: #93a5a0; background: #ecf0ed; border-color: #e4e9e5; }
QPushButton#primary { background: #087f79; border: 1px solid #087f79; color: #ffffff; padding: 12px 22px; }
QPushButton#primary:hover { background: #076c67; }
QPushButton#primary:disabled { background: #80aaa1; border-color: #80aaa1; }
QPushButton#smallPrimary { background: #087f79; border: 1px solid #087f79; color: #ffffff; }
QPushButton#smallPrimary:hover { background: #076c67; }
QPushButton#nav { background: transparent; border: 0; color: #b3cbca; text-align: left;
                 padding: 13px 16px; font-size: 14px; }
QPushButton#nav:hover { background: #23494b; color: #ffffff; }
QPushButton#nav:checked { background: #2e5555; color: #ffffff; }
QPushButton#nav:focus { border: 1px solid #88b6ad; }
QComboBox { border: 1px solid #dce5e1; border-radius: 8px; background: #f9fbfa;
            padding: 8px 10px; min-height: 20px; }
QComboBox:disabled { color: #869893; background: #f0f3f1; }
QLineEdit { border: 1px solid #dce5e1; border-radius: 8px; background: #f9fbfa;
            padding: 8px 10px; min-height: 20px; }
QLineEdit:focus { border: 1px solid #087f79; }
QLineEdit:disabled { color: #869893; background: #f0f3f1; }
QComboBox::drop-down { border: 0; width: 25px; }
QComboBox QAbstractItemView { background: white; selection-background-color: #e1f0e8; selection-color: #193438; }
QScrollArea { background: transparent; border: 0; }
QWidget#consoleList { background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 0; }
QScrollBar::handle:vertical { background: #c7d6cf; border-radius: 3px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QPlainTextEdit { background: #193438; color: #d1e5df; border: 0; border-radius: 12px;
                 padding: 16px; font-family: 'Cascadia Mono', 'Consolas'; font-size: 12px; }
"""


def label(text, name=None, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if name:
        widget.setObjectName(name)
    return widget


def column(widget, margins=0, spacing=12):
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return layout


def card(name='card'):
    frame = QFrame()
    frame.setObjectName(name)
    return frame


def badge(text, active=False):
    widget = label(text)
    widget.setStyleSheet('color: %s; background: %s; border-radius: 10px; padding: 5px 10px; '
                        'font-size: 11px; font-weight: 700;' %
                        (('#097266', '#d7ebe0') if active else ('#75837d', '#eaf0eb')))
    widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return widget


class SignalArt(QWidget):
    """Small original vector illustration; no external assets or network fonts."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False
        self.setMinimumSize(220, 175)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self.width() / 2, self.height() / 2)
        scale = min(self.width() / 310, self.height() / 205)
        painter.scale(scale, scale)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#d5e8dc'))
        painter.drawEllipse(QRectF(-98, -91, 190, 190))
        painter.setPen(QPen(QColor('#b9d4c4'), 1))
        for x in range(-130, 150, 18):
            for y in range(-75, 90, 18):
                painter.drawPoint(x, y)
        painter.save()
        painter.translate(-20, 16)
        painter.rotate(-27)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#b6ccbf'))
        painter.drawRoundedRect(QRectF(-77, -14, 154, 54), 20, 20)
        painter.setBrush(QColor('#b6c3c3'))
        painter.drawRoundedRect(QRectF(58, -18, 41, 33), 3, 3)
        painter.setBrush(QColor('#7a9290'))
        painter.drawRect(QRectF(76, -12, 7, 7))
        painter.drawRect(QRectF(76, 0, 7, 7))
        painter.setBrush(QColor('#fcfdfa'))
        painter.drawRoundedRect(QRectF(-84, -26, 150, 54), 18, 18)
        painter.setPen(QPen(QColor('#d8e3db'), 1))
        painter.drawLine(-67, 18, 51, 18)
        painter.setPen(QColor('#5b7671'))
        painter.setFont(QFont('Segoe UI', 9, QFont.Weight.DemiBold))
        painter.drawText(QRectF(-60, -12, 96, 25), Qt.AlignmentFlag.AlignCenter, 'Wi-Fi USB')
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#1aab83' if self.active else '#b7c9bc'))
        painter.drawEllipse(QRectF(-70, -5, 5, 5))
        painter.restore()
        painter.setPen(QPen(QColor('#168f80' if self.active else '#72a995'), 4,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for size in (24, 46, 68):
            painter.drawArc(QRectF(34 - size / 2, -66 - size / 2, size, size), 35 * 16, 110 * 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#168f80'))
        painter.drawEllipse(QRectF(31, -71, 6, 6))
        painter.end()


def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(TEAL))
    painter.drawRoundedRect(QRectF(0, 0, 64, 64), 16, 16)
    painter.setPen(QPen(QColor('#ffffff'), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    for size in (23, 43):
        painter.drawArc(QRectF(32 - size / 2, 39 - size / 2, size, size), 35 * 16, 110 * 16)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor('#ffffff'))
    painter.drawEllipse(QRectF(29, 38, 6, 6))
    painter.end()
    return QIcon(pixmap)


class Window(QMainWindow):
    def __init__(self, session=None):
        super().__init__()
        self.session = session or Session(parent=self)
        self.closing = False
        self.rows_signature = None
        self.setWindowTitle('WFC Connector')
        self.setWindowIcon(app_icon())
        self.resize(1160, 820)
        self.setMinimumSize(990, 760)
        arrow = (Path(__file__).parent / 'chevron.svg').as_posix()
        self.setStyleSheet(STYLE + '\nQComboBox::down-arrow { image: url("' + arrow + '"); width: 12px; height: 8px; }')
        base = QWidget()
        base.setObjectName('canvas')
        self.setCentralWidget(base)
        outer = QHBoxLayout(base)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = card('sidebar')
        sidebar.setFixedWidth(202)
        side = column(sidebar, 20, 8)
        side.addSpacing(14)
        logo = label('')
        logo.setPixmap(app_icon().pixmap(42, 42))
        side.addWidget(logo)
        side.addSpacing(5)
        side.addWidget(label('wfc connector', 'brand'))
        side.addWidget(label('OLD HARDWARE. NEW LIFE.', 'sideCaption'))
        side.addSpacing(36)
        self.navigation = QButtonGroup(self)
        for i, text in enumerate(('Overview', 'Activity', 'Setup')):
            button = QPushButton(text)
            button.setObjectName('nav')
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.navigation.addButton(button, i)
            side.addWidget(button)
        self.navigation.button(0).setChecked(True)
        side.addStretch()
        side.addWidget(label('MADE FOR THE ORIGINAL', 'sideCaption'))
        side.addWidget(label('Nintendo Wi-Fi\nUSB Connector', 'sideNote'))
        side.addSpacing(14)
        side.addWidget(label('Windows edition  /  Prototype', 'sideCaption'))
        side.addSpacing(4)
        outer.addWidget(sidebar)
        body = QWidget()
        layout = column(body, 30, 18)
        header = QHBoxLayout()
        headings = QVBoxLayout()
        headings.setSpacing(4)
        headings.addWidget(label('YOUR CONNECTION, REIMAGINED', 'eyebrow'))
        self.page_title = label('Connection overview', 'title')
        headings.addWidget(self.page_title)
        header.addLayout(headings)
        header.addStretch()
        self.header_badge = badge('SESSION OFF')
        header.addWidget(self.header_badge, alignment=Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)
        self.message = label('', 'message', True)
        self.message.hide()
        layout.addWidget(self.message)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.overview())
        self.pages.addWidget(self.activity())
        self.pages.addWidget(self.setup())
        layout.addWidget(self.pages, 1)
        footer = QHBoxLayout()
        footer.addWidget(label('Local connection. No account. Your hardware.', 'muted'))
        footer.addStretch()
        footer.addWidget(label('CLI compatible', 'muted'))
        layout.addLayout(footer)
        outer.addWidget(body, 1)
        self.navigation.idClicked.connect(self.navigate)
        self.session.changed.connect(self.render)
        self.session.line.connect(self.append_log)
        self.session.problem.connect(self.show_problem)
        self.session.finished.connect(self.worker_finished)
        self.load_settings()
        for line in self.session.logs:
            self.append_log(line)
        self.render()

    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        self.page_title.setText(('Connection overview', 'Session activity', 'Make yourself at home')[index])
        self.navigation.button(index).setChecked(True)

    def overview(self):
        page = QWidget()
        page.setObjectName('overview')
        layout = column(page, 0, 18)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        hero = card('hero')
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(26, 23, 18, 23)
        text = QVBoxLayout()
        text.setSpacing(12)
        self.hero_badge = badge('READY WHEN YOU ARE', True)
        text.addWidget(self.hero_badge)
        self.hero_title = label('Old consoles.\nNew connections.', 'heroTitle')
        text.addWidget(self.hero_title)
        self.hero_description = label('Plug in your connector. Bring your next adventure a little closer.', 'subtitle', True)
        self.hero_description.setMaximumWidth(370)
        text.addWidget(self.hero_description)
        text.addSpacing(3)
        actions = QHBoxLayout()
        self.start_button = QPushButton('Start connection')
        self.start_button.setObjectName('primary')
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.clicked.connect(self.toggle_session)
        actions.addWidget(self.start_button)
        actions.addStretch()
        text.addLayout(actions)
        hero_layout.addLayout(text, 3)
        self.art = SignalArt()
        hero_layout.addWidget(self.art, 2)
        layout.addWidget(hero)
        middle = QHBoxLayout()
        middle.setSpacing(18)
        consoles = card()
        consoles_layout = column(consoles, 20, 10)
        console_heading = QHBoxLayout()
        console_heading.addWidget(label('Your consoles', 'sectionTitle'))
        console_heading.addStretch()
        self.console_count = badge('0 SAVED')
        console_heading.addWidget(self.console_count)
        consoles_layout.addLayout(console_heading)
        consoles_layout.addWidget(label('You decide who gets to connect.', 'muted'))
        self.console_scroll = QScrollArea()
        self.console_scroll.setWidgetResizable(True)
        self.console_scroll.setMinimumHeight(175)
        consoles_layout.addWidget(self.console_scroll, 1)
        self.permission_note = label('On your console, select Nintendo Wi-Fi USB Connector\nto request permission.', 'muted', True)
        consoles_layout.addWidget(self.permission_note)
        middle.addWidget(consoles, 3)
        settings = card()
        settings.setMinimumWidth(245)
        form = column(settings, 20, 10)
        form.addWidget(label('This session', 'sectionTitle'))
        timing = QHBoxLayout()
        channel_box, duration_box = QVBoxLayout(), QVBoxLayout()
        channel_box.addWidget(label('CHANNEL', 'eyebrow'))
        self.channel = QComboBox()
        self.channel.setAccessibleName('Wireless channel')
        for channel in range(1, 12):
            self.channel.addItem('%d%s' % (channel, ' · Default' if channel == 6 else ''), channel)
        self.channel.setCurrentIndex(5)
        channel_box.addWidget(self.channel)
        duration_box.addWidget(label('AUTO-STOP', 'eyebrow'))
        self.duration = QComboBox()
        self.duration.setAccessibleName('Session duration')
        for title, seconds in [('15 minutes', 900), ('1 hour', 3600), ('4 hours', 14400), ('8 hours', 28800), ('24 hours', 86400)]:
            self.duration.addItem(title, seconds)
        self.duration.setCurrentIndex(2)
        duration_box.addWidget(self.duration)
        timing.addLayout(channel_box, 1)
        timing.addLayout(duration_box, 1)
        form.addLayout(timing)
        form.addWidget(label('DNS SERVICE', 'eyebrow'))
        self.dns_service = QComboBox()
        self.dns_service.setAccessibleName('DNS service')
        for title, address, _ in DNS_PRESETS:
            self.dns_service.addItem(title, address)
        self.dns_service.addItem('Custom DNS', 'custom')
        form.addWidget(self.dns_service)
        self.custom_dns = QLineEdit()
        self.custom_dns.setAccessibleName('Custom DNS IPv4 address')
        self.custom_dns.setPlaceholderText('IPv4 address, e.g. 178.62.43.212')
        self.custom_dns.setMaxLength(15)
        form.addWidget(self.custom_dns)
        self.dns_note = label('', 'muted', True)
        form.addWidget(self.dns_note)
        self.dns_service.currentIndexChanged.connect(self.update_dns_fields)
        self.update_dns_fields()
        form.addSpacing(6)
        form.addWidget(card('line'))
        metrics = QHBoxLayout()
        timer_box = QVBoxLayout()
        self.uptime = label('00:00', 'metric')
        timer_box.addWidget(self.uptime)
        timer_box.addWidget(label('Session time', 'muted'))
        frame_box = QVBoxLayout()
        self.traffic = label('0', 'metric')
        frame_box.addWidget(self.traffic)
        frame_box.addWidget(label('Data frames', 'muted'))
        metrics.addLayout(timer_box)
        metrics.addStretch()
        metrics.addLayout(frame_box)
        form.addLayout(metrics)
        form.addStretch()
        middle.addWidget(settings, 2)
        layout.addLayout(middle, 1)
        layout.addWidget(label('Game services are separate from the local connection and may need their own setup.', 'muted', True))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def activity(self):
        page = QWidget()
        layout = column(page)
        row = QHBoxLayout()
        row.addWidget(label('Live session details, without leaving the app.', 'subtitle'))
        row.addStretch()
        copy = QPushButton('Copy log')
        copy.clicked.connect(self.copy_log)
        row.addWidget(copy)
        layout.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setPlaceholderText('Session activity will appear here when you start the connector.')
        layout.addWidget(self.log_view, 1)
        layout.addWidget(label('Logs may include console names and MAC addresses. Review them before sharing.', 'muted', True))
        return page

    def setup(self):
        page = QWidget()
        layout = column(page, 0, 18)
        layout.addWidget(label('Three small steps. Then you are ready to connect.', 'subtitle'))
        for number, title, description in [
            ('01', 'Plug in the original connector', 'Connect the Nintendo Wi-Fi USB Connector directly to your PC. The supported USB ID is 0411:008B.'),
            ('02', 'Assign the WinUSB driver', 'Use Zadig to assign WinUSB to this adapter. This is a one-time setup; check the USB ID before replacing a driver.'),
            ('03', 'Prepare the network components', 'The app downloads verified components into this project. No changes to Windows Internet Connection Sharing are needed.'),
        ]:
            section = card()
            content = QHBoxLayout(section)
            content.setContentsMargins(20, 18, 20, 18)
            number_label = label(number, 'metric')
            number_label.setFixedWidth(48)
            content.addWidget(number_label)
            words = QVBoxLayout()
            words.addWidget(label(title, 'sectionTitle'))
            words.addWidget(label(description, 'muted', True))
            content.addLayout(words, 1)
            layout.addWidget(section)
        buttons = QHBoxLayout()
        self.setup_button = QPushButton('Install network components')
        self.setup_button.setObjectName('primary')
        self.setup_button.clicked.connect(self.session.install_runtime)
        guide = QPushButton('Open driver guide')
        guide.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ROOT / 'driver/README.md'))))
        buttons.addWidget(self.setup_button)
        buttons.addWidget(guide)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.runtime_label = label('', 'muted')
        layout.addWidget(self.runtime_label)
        layout.addStretch()
        layout.addWidget(label('The command line still works', 'sectionTitle'))
        layout.addWidget(label('python proto/ap.py run --channel 6 --seconds 14400', 'mono'))
        layout.addWidget(label('The app and CLI share saved permissions. Stop one session before starting the other. WEP is used for legacy compatibility; leave the connector off when you are not using it.', 'muted', True))
        return page

    def show_problem(self, text):
        self.message.setText(text)
        self.message.show()

    def append_log(self, text):
        self.log_view.appendPlainText(text)

    def copy_log(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.log_view.toPlainText())

    def load_settings(self):
        try:
            values = read_json(self.session.state / 'ui-settings.json', {})
            for combo, key in [(self.channel, 'channel'), (self.duration, 'seconds')]:
                index = combo.findData(values.get(key))
                if index >= 0:
                    combo.setCurrentIndex(index)
            saved_dns = normalize_dns(values.get('dns', 'system'))
            index = self.dns_service.findData(saved_dns)
            self.dns_service.setCurrentIndex(index if index >= 0 else self.dns_service.findData('custom'))
            self.custom_dns.setText(values.get('custom_dns', '') if index >= 0 else saved_dns)
        except (OSError, ValueError, AttributeError, TypeError):
            self.show_problem('Saved window settings could not be read. Check the session settings before starting.')

    def update_dns_fields(self):
        choice = self.dns_service.currentData()
        self.custom_dns.setVisible(choice == 'custom')
        note = next((note for _, address, note in DNS_PRESETS if address == choice),
                    'Forward console DNS queries to your chosen server.')
        self.dns_note.setText((choice + ' · ' if choice not in ('system', 'custom') else '') + note)

    def selected_dns(self):
        choice = self.dns_service.currentData()
        value = self.custom_dns.text().strip() if choice == 'custom' else choice
        if choice == 'custom' and value == 'system':
            raise ValueError('Enter an IPv4 address, or select Windows DNS.')
        return normalize_dns(value)

    def toggle_session(self):
        if self.session.busy:
            self.session.stop()
            return
        self.message.hide()
        try:
            dns = self.selected_dns()
            write_json(self.session.state / 'ui-settings.json', {
                'channel': self.channel.currentData(), 'seconds': self.duration.currentData(),
                'dns': dns, 'custom_dns': self.custom_dns.text().strip()})
        except ValueError as exc:
            self.show_problem(str(exc))
            self.custom_dns.setFocus()
            return
        except OSError as exc:
            self.show_problem('Could not save settings: ' + str(exc))
            return
        self.session.start(self.channel.currentData(), self.duration.currentData(), dns=dns)

    def render(self):
        mode = self.session.mode
        busy = self.session.busy
        live = mode == 'running'
        waiting = live and self.session.last_event is not None and time.monotonic() - self.session.last_event > 5
        self.header_badge.setText({'idle': 'SESSION OFF', 'starting': 'STARTING', 'running': 'RADIO ACTIVE',
                                  'stopping': 'STOPPING', 'installing': 'SETTING UP', 'error': 'NEEDS ATTENTION'}[mode])
        self.header_badge.setStyleSheet('color: %s; background: %s; border-radius: 10px; padding: 5px 10px; '
                                       'font-size: 11px; font-weight: 700;' %
                                       (('#087366', '#d7ebe0') if live else ('#75837d', '#eaf0eb')))
        self.hero_badge.setText('WAITING FOR UPDATE' if waiting else 'BROADCASTING' if live else 'READY WHEN YOU ARE')
        self.hero_title.setText('Your connector\nis live.' if live else 'Old consoles.\nNew connections.')
        self.hero_description.setText('Select Nintendo Wi-Fi USB Connector on your console to join.' if live else
                                      'Plug in your connector. Bring your next adventure a little closer.')
        self.start_button.setText({'idle': 'Start connection', 'starting': 'Starting…', 'running': 'Stop connection',
                                  'stopping': 'Stopping…', 'installing': 'Installing…', 'error': 'Try again'}[mode])
        self.start_button.setEnabled(mode not in ('starting', 'stopping', 'installing'))
        self.channel.setEnabled(not busy)
        self.duration.setEnabled(not busy)
        self.dns_service.setEnabled(not busy)
        self.custom_dns.setEnabled(not busy)
        self.setup_button.setEnabled(not busy)
        self.setup_button.setText('Verify network components' if self.session.runtime_ready else 'Install network components')
        self.runtime_label.setText('Network components are installed.' if self.session.runtime_ready else
                                   'Network components are not installed yet.')
        self.art.active = live
        self.art.update()
        seconds = int(time.monotonic() - self.session.started_at) if self.session.started_at else 0
        self.uptime.setText('%02d:%02d' % (seconds // 60, seconds % 60) if seconds < 3600 else
                            '%d:%02d:%02d' % (seconds // 3600, seconds // 60 % 60, seconds % 60))
        counts = self.session.telemetry.get('counters', {})
        self.traffic.setText(format(counts.get('data_rx', 0) + counts.get('data_tx', 0), ','))
        if self.session.store_error:
            self.show_problem(self.session.store_error)
        self.render_consoles(live and not waiting)

    def render_consoles(self, live):
        connected = {item['mac'] for item in self.session.telemetry.get('stations', []) if item.get('associated')} if live else set()
        waiting = set(self.session.pending) - self.session.allowed if live else set()
        all_macs = waiting | self.session.allowed
        rows = []
        for mac in sorted(all_macs, key=lambda value: (value not in waiting, value)):
            name = self.session.pending.get(mac, {}).get('name') or self.session.names.get(mac) or 'Nintendo console'
            state = 'Needs permission' if mac in waiting else 'Associated' if mac in connected else 'Allowed'
            rows.append((mac, str(name), state))
        signature = (tuple(rows), bool(self.session.store_error))
        self.console_count.setText('%d SAVED' % len(self.session.allowed))
        self.permission_note.setText('%d console%s waiting for your permission.' % (len(waiting), '' if len(waiting) == 1 else 's') if waiting else
                                     'On your console, select Nintendo Wi-Fi USB Connector\nto request permission.')
        if signature == self.rows_signature:
            return
        self.rows_signature = signature
        container = QWidget()
        container.setObjectName('consoleList')
        items = column(container, 0, 8)
        if not rows:
            items.addStretch()
            empty = label('A place for your consoles', 'sectionTitle')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            items.addWidget(empty)
            detail = label('Start a connection, then search from your console.\nIts request will appear here.', 'muted', True)
            detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
            items.addWidget(detail)
            items.addStretch()
        for mac, name, state in rows:
            row = card('row')
            line = QHBoxLayout(row)
            line.setContentsMargins(12, 12, 12, 12)
            words = QVBoxLayout()
            words.setSpacing(3)
            title = label(name[:40], 'consoleName')
            title.setToolTip(name)
            title.setMinimumWidth(0)
            title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            words.addWidget(title)
            words.addWidget(label(mac.upper(), 'mono'))
            status_label = label(state, 'muted')
            status_label.setStyleSheet('color: %s;' % ('#b1762c' if state == 'Needs permission' else TEAL))
            words.addWidget(status_label)
            line.addLayout(words, 1)
            pending = state == 'Needs permission'
            action = QPushButton('Allow' if pending else 'Revoke')
            action.setObjectName('smallPrimary' if pending else 'secondary')
            action.setAccessibleName(('Allow ' if pending else 'Revoke ') + name)
            action.setEnabled(not self.session.store_error)
            action.clicked.connect(lambda checked=False, address=mac, allow=pending: self.session.set_permission(address, allow))
            line.addWidget(action)
            items.addWidget(row)
        if rows:
            items.addStretch()
        old = self.console_scroll.takeWidget()
        self.console_scroll.setWidget(container)
        if old:
            old.deleteLater()

    def closeEvent(self, event):
        if self.session.busy:
            self.closing = True
            self.session.stop()
            self.show_problem('Stopping your session and releasing the adapter…')
            event.ignore()
        else:
            event.accept()

    def worker_finished(self):
        if self.closing:
            self.close()
