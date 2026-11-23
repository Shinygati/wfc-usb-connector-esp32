"""Render the actual desktop widgets with temporary state, without opening USB."""
import argparse
import os
from pathlib import Path
import sys
import tempfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from desktop.session import Session
from desktop.window import Window
from proto.dns_config import normalize_dns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dns', type=normalize_dns, default='system')
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/desktop-preview.png')
    parser.add_argument('--width', type=int, default=1160)
    parser.add_argument('--height', type=int, default=820)
    args = parser.parse_args()
    app = QApplication([])
    app.setStyle('Fusion')
    for name in ('segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf', 'consola.ttf'):
        path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    with tempfile.TemporaryDirectory() as state:
        session = Session(state)
        window = Window(session)
        index = window.dns_service.findData(args.dns)
        window.dns_service.setCurrentIndex(index if index >= 0 else window.dns_service.findData('custom'))
        if index < 0:
            window.custom_dns.setText(args.dns)
        window.resize(args.width, args.height)
        window.show()
        app.processEvents()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if not window.grab().save(str(args.output)):
            raise RuntimeError('Could not save desktop preview.')
        window.close()
    print('Desktop preview saved to ' + str(args.output))


if __name__ == '__main__':
    main()
