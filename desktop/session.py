"""Own one CLI child process and share its existing permission files."""
from collections import deque
import codecs
import json
from pathlib import Path
import sys
import time

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from proto.dns_config import normalize_dns

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / 'proto' / 'state'


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def normalize_mac(value):
    data = bytes.fromhex(value.replace(':', '').replace('-', ''))
    if len(data) != 6 or data[0] & 1 or data == b'\0' * 6:
        raise ValueError('Invalid console address.')
    return data.hex(':')


def python_executable():
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    return str(executable)


class Session(QObject):
    changed = Signal()
    line = Signal(str)
    problem = Signal(str)
    finished = Signal()

    def __init__(self, state=DEFAULT_STATE, parent=None, command=None):
        super().__init__(parent)
        self.state = Path(state).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.command = command  # Injectable worker for process lifecycle tests.
        self.mode = 'idle'
        self.role = 'ap'
        self.error = ''
        self.buffer = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.logs = deque(maxlen=500)
        self.telemetry = {}
        self.started_at = None
        self.last_event = None
        self.pending = {}
        self.allowed = set()
        self.names = {}
        self.store_error = ''
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(ROOT))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert('PYTHONIOENCODING', 'utf-8')
        self.process.setProcessEnvironment(environment)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.on_finished)
        self.process.errorOccurred.connect(self.on_error)
        self.stop_deadline = QTimer(self)
        self.stop_deadline.setSingleShot(True)
        self.stop_deadline.timeout.connect(self.force_stop)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(750)
        self.refresh()

    @property
    def busy(self):
        return self.process.state() != QProcess.ProcessState.NotRunning

    @property
    def runtime_ready(self):
        return (ROOT / 'runtime/ucrt64/bin/libslirp-0.dll').is_file()

    def refresh(self):
        try:
            pending = read_json(self.state / 'pending.json', {})
            names = read_json(self.state / 'ui-names.json', {})
            allowed = read_json(self.state / 'allow.json', [])
            if not isinstance(pending, dict) or not isinstance(names, dict) or not isinstance(allowed, list):
                raise ValueError('Unexpected permission file format.')
            parsed = {}
            for mac, item in pending.items():
                if not isinstance(item, dict):
                    raise ValueError('Unexpected pending console format.')
                parsed[normalize_mac(mac)] = item
            self.pending = parsed
            self.allowed = {normalize_mac(mac) for mac in allowed}
            self.names = {normalize_mac(mac): str(name) for mac, name in names.items()}
            self.store_error = ''
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.store_error = 'Could not read console permissions. Check Activity before making changes.'
            # Preserve the last good view and never overwrite damaged state.
            if not self.logs or str(exc) not in self.logs[-1]:
                self.add_log('Permission file error: ' + str(exc))
        self.changed.emit()

    def set_permission(self, mac, allow):
        try:
            mac = normalize_mac(mac)
            self.refresh()
            if self.store_error:
                raise ValueError(self.store_error)
            allowed = set(self.allowed)
            if allow:
                allowed.add(mac)
            else:
                allowed.discard(mac)
            if len(allowed) > 32:
                raise ValueError('The connector supports up to 32 approved consoles.')
            name = self.pending.get(mac, {}).get('name')
            if name:
                self.names[mac] = str(name)
                write_json(self.state / 'ui-names.json', self.names)
            write_json(self.state / 'allow.json', sorted(allowed))
            self.add_log(('Allowed ' if allow else 'Revoked ') + mac)
            self.refresh()
            return True
        except (OSError, ValueError, TypeError) as exc:
            self.problem.emit(str(exc))
            return False

    def start(self, channel=6, seconds=14400, dns='system'):
        if self.busy:
            return
        try:
            dns = normalize_dns(dns)
        except ValueError as exc:
            self.problem.emit(str(exc))
            return
        if channel not in range(1, 12) or seconds not in (900, 3600, 14400, 28800, 86400):
            self.problem.emit('Choose a valid channel and session length.')
            return
        if not self.runtime_ready and self.command is None:
            self.problem.emit('Install the network components first using the Setup button.')
            return
        self.role = 'ap'
        self.mode = 'starting'
        self.error = ''
        self.telemetry = {}
        self.started_at = None
        self.last_event = None
        self.buffer = ''
        self.decoder.reset()
        arguments = ['-u', str(ROOT / 'proto/ap.py'), '--state', str(self.state),
                     'run', '--channel', str(channel), '--seconds', str(seconds), '--dns', dns, '--events']
        self.add_log('Starting connector on channel %d; DNS: %s.' % (channel, dns))
        self.process.start(python_executable(), self.command or arguments)
        self.changed.emit()

    def install_runtime(self):
        if self.busy:
            return
        self.role = 'setup'
        self.mode = 'installing'
        self.error = ''
        self.buffer = ''
        self.decoder.reset()
        self.add_log('Downloading and verifying network components from MSYS2.')
        self.process.start(python_executable(), ['-u', str(ROOT / 'tools/fetch_slirp.py')])
        self.changed.emit()

    def stop(self):
        if not self.busy or self.mode == 'stopping':
            return
        self.mode = 'stopping'
        if self.role == 'setup':
            self.process.kill()
        elif self.last_event is not None:
            try:
                (self.state / 'stop').write_text('stop\n', encoding='ascii')
            except OSError as exc:
                self.add_log('Could not request a graceful stop: ' + str(exc))
        if self.role == 'ap':
            self.stop_deadline.start(12000)
        self.changed.emit()

    def force_stop(self):
        if self.busy:
            self.add_log('Worker did not stop in time; ending this session process.')
            self.process.kill()

    def add_log(self, message):
        self.logs.append(message)
        self.line.emit(message)

    def read_output(self):
        self.buffer += self.decoder.decode(bytes(self.process.readAllStandardOutput()))
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            self.accept_line(line.rstrip('\r'))
        if len(self.buffer) > 65536:
            self.add_log(self.buffer[:65536])
            self.buffer = ''

    def accept_line(self, line):
        if line.startswith('@WFC '):
            try:
                event = json.loads(line[5:])
                if not isinstance(event, dict) or event.get('event') != 'status':
                    return
                self.telemetry = event
                self.last_event = time.monotonic()
                if self.mode == 'starting':
                    self.mode = 'running'
                    self.started_at = self.last_event
                # Stop may have been requested during USB initialization, before
                # the CLI clears a previous stop file. Reissue after readiness.
                if self.mode == 'stopping':
                    (self.state / 'stop').write_text('stop\n', encoding='ascii')
                self.changed.emit()
            except (ValueError, OSError) as exc:
                self.add_log('Session event error: ' + str(exc))
            return
        self.add_log(line)
        if 'Access denied' in line or 'Resource busy' in line:
            self.error = 'The adapter is busy. Stop any CLI session using it, then try again.'
        elif 'Connector not found' in line:
            self.error = 'Adapter not found. Plug it in and check the WinUSB driver in Setup.'
        elif 'missing libslirp runtime' in line:
            self.error = 'Network components are missing. Install them using Setup.'

    def on_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.error = 'Could not start Python: ' + self.process.errorString()
            self.mode = 'error'
            self.problem.emit(self.error)
            self.changed.emit()
            self.finished.emit()

    def on_finished(self, code, status):
        self.stop_deadline.stop()
        self.read_output()
        self.buffer += self.decoder.decode(b'', final=True)
        if self.buffer:
            self.accept_line(self.buffer)
            self.buffer = ''
        was_stopping = self.mode == 'stopping'
        self.mode = 'idle' if code == 0 or was_stopping else 'error'
        if self.mode == 'error':
            self.error = self.error or 'The session ended unexpectedly. Open Activity for details.'
            self.problem.emit(self.error)
        elif self.role == 'setup' and not was_stopping:
            self.add_log('Network components are ready.')
        self.telemetry = {}
        self.started_at = None
        self.changed.emit()
        self.finished.emit()
