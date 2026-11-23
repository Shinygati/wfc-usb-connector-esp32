"""Optional desktop tests. The CLI suite can run without Qt installed."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from PySide6.QtCore import QProcess, Qt
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QPushButton
    from desktop.session import Session, write_json
    from desktop.window import Window
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, 'optional desktop dependencies are not installed')
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        # The Windows offscreen plugin does not discover system fonts itself.
        for name in ('segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf', 'consola.ttf'):
            font = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name
            if font.exists():
                QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = Path(self.folder.name)
        self.session = Session(self.state)
        self.window = Window(self.session)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.session.refresh_timer.stop()
        if self.session.busy:
            self.session.process.kill()
            self.session.process.waitForFinished(3000)
        self.window.close()
        self.window.deleteLater()
        self.session.deleteLater()
        self.app.processEvents()
        self.folder.cleanup()

    def wait_for(self, predicate, timeout=5000):
        deadline = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(predicate())

    def worker(self, delayed=False):
        script = self.state / 'worker.py'
        script.write_text(
            'from pathlib import Path\nimport json, time\n'
            'state = Path(__file__).parent\n'
            + ('time.sleep(.15)\n(state / "stop").unlink(missing_ok=True)\n' if delayed else '')
            + 'print("@WFC " + json.dumps({"event":"status", "counters":{}, "stations":[]}), flush=True)\n'
            'while not (state / "stop").exists(): time.sleep(.01)\n'
            'print("RADIO OFF", flush=True)\n', encoding='utf-8')
        self.session.command = ['-u', str(script)]

    def test_permission_buttons_share_cli_files_and_keep_names_plain_text(self):
        mac = '02:11:22:33:44:55'
        write_json(self.state / 'pending.json', {mac: {'name': '<b>Pearl</b>'}})
        self.session.mode = 'running'
        self.session.refresh()
        allow = next(b for b in self.window.findChildren(QPushButton) if b.text() == 'Allow')
        QTest.mouseClick(allow, Qt.MouseButton.LeftButton)
        self.assertEqual(json.loads((self.state / 'allow.json').read_text()), [mac])
        self.assertEqual(self.session.names[mac], '<b>Pearl</b>')
        revoke = next(b for b in self.window.findChildren(QPushButton) if b.text() == 'Revoke')
        QTest.mouseClick(revoke, Qt.MouseButton.LeftButton)
        self.assertEqual(json.loads((self.state / 'allow.json').read_text()), [])

    def test_malformed_permissions_are_not_overwritten(self):
        path = self.state / 'allow.json'
        path.write_text('damaged')
        self.assertFalse(self.session.set_permission('02:11:22:33:44:55', True))
        self.assertEqual(path.read_text(), 'damaged')

    def test_start_stop_runs_worker_and_releases_it(self):
        self.worker()
        QTest.mouseClick(self.window.start_button, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: self.session.mode == 'running')
        self.assertFalse(self.window.channel.isEnabled())
        QTest.mouseClick(self.window.start_button, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: not self.session.busy)
        self.assertEqual(self.session.mode, 'idle')
        self.assertTrue(self.window.channel.isEnabled())
        self.assertIn('RADIO OFF', self.window.log_view.toPlainText())

    def test_closing_during_start_stops_only_owned_worker(self):
        self.worker(delayed=True)
        self.session.start()
        self.window.close()
        self.wait_for(lambda: not self.session.busy)
        self.assertFalse(self.window.isVisible())

    def test_external_cli_busy_error_does_not_send_stop(self):
        self.session.command = ['-c', 'import sys; print("USBError: Access denied"); sys.exit(1)']
        self.session.start()
        self.wait_for(lambda: self.session.mode == 'error')
        self.assertIn('Stop any CLI session', self.window.message.text())
        self.assertFalse((self.state / 'stop').exists())

    def test_close_before_readiness_does_not_stop_an_external_cli(self):
        self.session.command = ['-c', 'import time; time.sleep(.2); print("USBError: Access denied"); raise SystemExit(1)']
        self.session.start()
        self.window.close()
        self.wait_for(lambda: not self.session.busy)
        self.assertFalse((self.state / 'stop').exists())

    def test_telemetry_must_report_readiness_and_malformed_event_is_ignored(self):
        self.session.mode = 'starting'
        self.session.accept_line('A human-readable log line')
        self.assertEqual(self.session.mode, 'starting')
        self.session.accept_line('@WFC invalid')
        self.session.accept_line('@WFC []')
        self.assertEqual(self.session.mode, 'starting')
        self.session.accept_line('@WFC {"event":"status", "counters":{"data_rx":7,"data_tx":4},"stations":[]}')
        self.assertEqual(self.session.mode, 'running')
        self.assertEqual(self.window.traffic.text(), '11')

    def test_settings_survive_window_restart(self):
        write_json(self.state / 'ui-settings.json', {'channel': 11, 'seconds': 3600})
        self.window.load_settings()
        self.assertEqual(self.window.channel.currentData(), 11)
        self.assertEqual(self.window.duration.currentData(), 3600)

    def test_kaeru_selection_is_saved_and_passed_to_worker(self):
        self.window.dns_service.setCurrentIndex(1)
        with patch.object(self.session, 'start') as start:
            self.window.toggle_session()
            start.assert_called_once_with(6, 14400, dns='178.62.43.212')
        settings = json.loads((self.state / 'ui-settings.json').read_text())
        self.assertEqual(settings['dns'], '178.62.43.212')
        self.window.dns_service.setCurrentIndex(0)
        self.window.load_settings()
        self.assertEqual(self.window.selected_dns(), '178.62.43.212')

    def test_custom_dns_is_validated_before_saving_or_starting(self):
        self.window.dns_service.setCurrentIndex(self.window.dns_service.findData('custom'))
        for value in ('', 'example.org', '1.2.3', '999.1.1.1', '0.0.0.0', '224.0.0.1', 'system'):
            self.window.custom_dns.setText(value)
            with patch.object(self.session, 'start') as start:
                self.window.toggle_session()
                start.assert_not_called()
            self.assertFalse((self.state / 'ui-settings.json').exists())
        self.window.custom_dns.setText('192.0.2.53')
        with patch.object(self.session, 'start') as start:
            self.window.toggle_session()
            start.assert_called_once_with(6, 14400, dns='192.0.2.53')
        self.window.dns_service.setCurrentIndex(0)
        self.window.load_settings()
        self.assertEqual(self.window.dns_service.currentData(), 'custom')
        self.assertEqual(self.window.selected_dns(), '192.0.2.53')

    def test_session_command_includes_selected_dns(self):
        self.session.command = None
        with patch.object(Session, 'runtime_ready', True), patch.object(self.session.process, 'start') as start:
            self.session.start(dns='178.62.43.212')
        arguments = start.call_args.args[1]
        self.assertEqual(arguments[arguments.index('--dns') + 1], '178.62.43.212')

    def test_dns_controls_are_locked_during_session(self):
        self.worker()
        self.session.start()
        self.wait_for(lambda: self.session.mode == 'running')
        self.assertFalse(self.window.dns_service.isEnabled())
        self.assertFalse(self.window.custom_dns.isEnabled())
        self.session.stop()
        self.wait_for(lambda: not self.session.busy)
        self.assertTrue(self.window.dns_service.isEnabled())

    def test_navigation_and_minimum_size_keep_controls_reachable(self):
        self.window.resize(990, 760)
        self.app.processEvents()
        for index in (1, 2, 0):
            QTest.mouseClick(self.window.navigation.button(index), Qt.MouseButton.LeftButton)
            self.assertEqual(self.window.pages.currentIndex(), index)
        self.assertTrue(self.window.start_button.isVisible())
        self.assertFalse(self.window.grab().isNull())


if __name__ == '__main__':
    unittest.main()
