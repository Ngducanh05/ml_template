"""Pinned, idempotent Windows amd64 bootstrap. Source, cache and installs stay separate."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
from urllib.request import urlopen
import zipfile

HERE = Path(__file__).resolve().parent
CACHE = HERE / '.cache'
LOCAL = HERE / '.local'
RELEASES = {
    'prometheus': {
        'version': '3.15.0',
        'url': 'https://github.com/prometheus/prometheus/releases/download/v3.15.0/prometheus-3.15.0.windows-amd64.zip',
        'sha256': '5d333b385557d9adc2ff015d13da9809baccc52fb800a82d1e5c94b79258f87e',
        'names': ['prometheus.exe'],
    },
    'grafana': {
        'version': '12.2.0',
        'url': 'https://dl.grafana.com/oss/release/grafana-12.2.0.windows-amd64.zip',
        'sha256': 'e5d1bde97dd6f0920398c5cc3f03eebe314dcb6f20f04b0a94dcdea744b76780',
        'names': ['grafana-server.exe', 'grafana.exe'],
    },
}
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def require_windows():
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('This demo requires Windows amd64 and the existing repository Python environment.')


def validate_pe(path):
    path = Path(path)
    if path.suffix.lower() != '.exe': raise RuntimeError(f'Expected Windows .exe: {path}')
    with path.open('rb') as f:
        if f.read(2) != b'MZ': raise RuntimeError(f'Not a Windows executable: {path}')
        f.seek(0x3c)
        offset = struct.unpack('<I', f.read(4))[0]
        f.seek(offset)
        if f.read(4) != b'PE\0\0' or f.read(2) != b'\x64\x86':
            raise RuntimeError(f'Expected Windows amd64 PE executable: {path}')


def locate(name, root=None):
    root = Path(root) if root else LOCAL / name
    spec = RELEASES[name]
    archive = CACHE / name / spec['url'].rsplit('/', 1)[1]
    detail = f"URL: {spec['url']}\nArchive: {archive}\nExtracted root: {root}\nSearched: {', '.join('**/'+x for x in spec['names'])}"
    try:
        executable = None
        for filename in spec['names']:
            matches = list(root.rglob(filename))
            if name == 'grafana': matches = [x for x in matches if x.parent.name.lower() == 'bin']
            if len(matches) > 1: raise RuntimeError(f'Multiple {filename} installations: {matches}')
            if matches:
                executable = matches[0]
                break
        if executable is None: raise RuntimeError('Windows server executable is missing')
        validate_pe(executable)
        home = executable.parent
        if name == 'grafana':
            home = executable.parent.parent
            if not (home/'conf'/'defaults.ini').is_file() or not (home/'public'/'build').is_dir():
                raise RuntimeError('Invalid Grafana home: conf/defaults.ini and public/build are required')
            # The compatibility launcher delegates to grafana.exe in current releases.
            unified = home/'bin'/'grafana.exe'
            if unified.exists(): validate_pe(unified)
        return executable, home
    except (OSError, ValueError, struct.error, RuntimeError) as exc:
        raise RuntimeError(f'{name} bootstrap validation failed: {exc}\n{detail}') from exc


def check_version(name, executable):
    result = subprocess.run([str(executable), '--version'], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=20, creationflags=FLAGS)
    output = result.stdout + result.stderr
    if result.returncode or not re.search(r'(?<![\d.])'+re.escape(RELEASES[name]['version'])+r'(?![\d.])', output):
        raise RuntimeError(f'{name} version check failed ({result.returncode}): {output[-1500:]}')
    return output.strip()


def validate_installation(name):
    executable, home = locate(name)
    receipt_path = LOCAL/name/'installation.json'
    try:
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        spec = RELEASES[name]
        if any(receipt.get(k) != spec[k] for k in ('version','url','sha256')): raise ValueError('release mismatch')
        for rel, expected in receipt['files'].items():
            path = (LOCAL/name/rel).resolve()
            if not path.is_relative_to((LOCAL/name).resolve()) or digest(path) != expected:
                raise ValueError('installed file hash mismatch: '+rel)
        if str(executable.relative_to(LOCAL/name)) not in receipt['files']: raise ValueError('executable not recorded')
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f'{name} installation is not verified; run python canary/bootstrap.py: {exc}') from exc
    check_version(name, executable)
    return executable, home


def ensure(name):
    require_windows()
    try:
        result = validate_installation(name)
        print(f'{name}: reuse verified {RELEASES[name]["version"]}', flush=True)
        return result
    except RuntimeError: pass
    spec = RELEASES[name]
    cache = CACHE/name
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache/spec['url'].rsplit('/', 1)[1]
    if not archive.exists():
        partial = archive.with_suffix('.zip.partial')
        print(f'{name}: download {spec["url"]}', flush=True)
        with urlopen(spec['url'], timeout=60) as source, partial.open('wb') as dest:
            shutil.copyfileobj(source, dest, length=1024*1024)
        if digest(partial) != spec['sha256']:
            partial.unlink()
            raise RuntimeError(f'Checksum mismatch: {spec["url"]}')
        partial.replace(archive)
    if digest(archive) != spec['sha256']: raise RuntimeError(f'Cached archive checksum mismatch: {archive}')
    target = (LOCAL/name).resolve()
    if target.parent != LOCAL.resolve() or name not in RELEASES: raise RuntimeError('Unsafe extraction destination')
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            path = (target/member.filename).resolve()
            if not path.is_relative_to(target) or ':' in member.filename or ((member.external_attr >> 16) & 0o170000) == 0o120000:
                raise RuntimeError('Unsafe archive member: '+member.filename)
        if target.exists(): shutil.rmtree(target)
        target.mkdir(parents=True)
        z.extractall(target)
    executable, home = locate(name)
    version_output = check_version(name, executable)
    files = [executable]
    if name == 'grafana':
        files += [home/'conf'/'defaults.ini']
        if (home/'bin'/'grafana.exe').is_file(): files.append(home/'bin'/'grafana.exe')
    receipt = {k:spec[k] for k in ('version','url','sha256')}
    receipt.update(platform='windows', architecture='amd64', archive_type='zip', version_output=version_output,
                   files={str(x.relative_to(target)):digest(x) for x in files})
    (target/'installation.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(f'{name}: verified and installed at {target}', flush=True)
    return executable, home


if __name__ == '__main__':
    require_windows()
    for component in RELEASES: ensure(component)
