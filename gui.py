"""Launch the optional WFC Connector desktop app."""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, help='override the shared CLI state directory')
    args = parser.parse_args()
    try:
        from PySide6.QtCore import QLockFile
        from PySide6.QtWidgets import QApplication, QMessageBox
        from desktop.session import DEFAULT_STATE, Session
        from desktop.window import Window
    except ImportError as exc:
        print('Desktop dependencies are missing. Run: python -m pip install -r requirements-gui.txt', file=sys.stderr)
        raise SystemExit(1) from exc
    app = QApplication(sys.argv[:1])
    app.setApplicationName('WFC Connector')
    app.setStyle('Fusion')
    state = (args.state or DEFAULT_STATE).resolve()
    state.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(state / 'desktop.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, 'WFC Connector', 'The desktop app is already open for this state directory.')
        return 1
    session = Session(state)
    window = Window(session)
    window.show()
    try:
        return app.exec()
    finally:
        lock.unlock()


if __name__ == '__main__':
    sys.exit(main())
