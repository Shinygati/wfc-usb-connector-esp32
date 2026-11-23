"""Download a private libslirp runtime from MSYS2, verifying package SHA-256.

Only DLLs, public headers and license files are extracted into runtime/.
No system packages or Windows network configuration are modified.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'runtime'
MANIFEST = Path(__file__).with_name('slirp-packages.json')
NAMES = ['libslirp', 'glib2', 'gettext-runtime', 'libiconv', 'pcre2', 'libwinpthread']


def main():
    DEST.mkdir(exist_ok=True)
    if MANIFEST.exists():
        packages = json.loads(MANIFEST.read_text())
    else:
        packages = []
        for name in NAMES:
            page = urllib.request.urlopen('https://packages.msys2.org/packages/mingw-w64-ucrt-x86_64-' + name, timeout=30).read().decode()
            url = re.search(r'https://mirror.msys2.org/mingw/ucrt64/[^"<> ]+pkg.tar.zst', page).group()
            digest = re.search(r'[a-f0-9]{64}', page).group()
            packages.append({'name': name, 'url': url, 'sha256': digest})
        MANIFEST.write_text(json.dumps(packages, indent=2) + '\n')
    for package in packages:
        archive = DEST / (package['name'] + '.pkg.tar.zst')
        if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != package['sha256']:
            archive.write_bytes(urllib.request.urlopen(package['url'], timeout=60).read())
        if hashlib.sha256(archive.read_bytes()).hexdigest() != package['sha256']:
            raise RuntimeError('SHA-256 mismatch: ' + package['name'])
        names = subprocess.check_output(['tar', '-tf', str(archive)], text=True).splitlines()
        selected = [n for n in names if
                    (n.startswith('ucrt64/bin/') and n.endswith('.dll')) or
                    (n.startswith('ucrt64/include/slirp/') and n.endswith('.h')) or
                    (n.startswith('ucrt64/share/licenses/') and not n.endswith('/'))]
        if any('..' in Path(n).parts or ':' in n or '\\' in n for n in selected):
            raise RuntimeError('unsafe archive path')
        with tempfile.TemporaryDirectory(prefix='extract-', dir=DEST) as temporary:
            subprocess.run(['tar', '-xf', str(archive), '-C', temporary, *selected], check=True)
            for name in selected:
                source = Path(temporary) / name
                target = DEST / name
                if target.exists() and source.read_bytes() == target.read_bytes():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(source, target)
                except PermissionError as exc:
                    raise RuntimeError('Close the AP before updating runtime file: ' + name) from exc
        print(package['name'] + ': verified and extracted', flush=True)


if __name__ == '__main__':
    main()
