"""Offline voice installer (#39): fetch pinned assets once, verify SHA-256, check hardware.

    python scripts/provision_voice.py             # download missing assets, verify all, hardware check
    python scripts/provision_voice.py --verify    # no network: exit 1 if anything is missing or altered
    python scripts/provision_voice.py --hardware  # audio devices and Python packages only

Never overwrites an asset whose hash is wrong; delete it and run again. The voice worker
itself never downloads; this script is the only place assets are fetched.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'voice' / 'assets.json'
CHUNK = 1 << 20
PACKAGES = ['numpy', 'sounddevice', 'onnxruntime', 'faster_whisper', 'openwakeword', 'vosk', 'piper', 'webrtcvad']


class AssetError(Exception):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def resolve_dest(dest, root=ROOT):
    if dest == '@openwakeword':
        spec = importlib.util.find_spec('openwakeword')
        if spec is None or not spec.submodule_search_locations:
            raise AssetError('openwakeword is not installed; install voice/requirements.txt first.')
        return Path(spec.submodule_search_locations[0]) / 'resources' / 'models'
    return root / dest


def check_file(path, entry):
    """Return None when the file matches, otherwise a reason."""
    if not path.is_file():
        return 'missing'
    actual = sha256(path)
    if actual != entry['sha256']:
        return f"SHA-256 mismatch: expected {entry['sha256']}, got {actual}"
    return None


def verify(manifest, root=ROOT):
    """Hash every asset without touching the network. Returns [(group, path, problem)]."""
    problems = []
    for group in manifest['groups']:
        try:
            dest = resolve_dest(group['dest'], root)
        except AssetError as exc:
            problems.append((group['name'], group['dest'], str(exc)))
            continue
        for entry in group['files']:
            problem = check_file(dest / entry['path'], entry)
            if problem:
                problems.append((group['name'], dest / entry['path'], problem))
    return problems


def download(url, target, expected_sha, expected_size=None):
    """Stream url to target.part, hashing as it goes; only a matching file is renamed into place."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + '.part')
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as response, open(part, 'wb') as out:
            for block in iter(lambda: response.read(CHUNK), b''):
                digest.update(block)
                out.write(block)
        if expected_size is not None and part.stat().st_size != expected_size:
            raise AssetError(f'{url}: size {part.stat().st_size}, expected {expected_size}')
        if digest.hexdigest() != expected_sha:
            raise AssetError(f'{url}: SHA-256 mismatch: expected {expected_sha}, got {digest.hexdigest()}')
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)


def extract_archive(group, dest, missing, log):
    archive = group['archive']
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / 'archive.zip'
        log(f"  fetching {archive['url']}")
        download(archive['url'], zip_path, archive['sha256'], archive.get('size'))
        with zipfile.ZipFile(zip_path) as zf:
            # Only manifest paths are extracted, so archive entries can't escape dest.
            for entry in missing:
                target = dest / entry['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry['path']) as src, open(target, 'wb') as out:
                    shutil.copyfileobj(src, out, CHUNK)
                problem = check_file(target, entry)
                if problem:
                    target.unlink()
                    raise AssetError(f'{target}: {problem}')


def provision(manifest, root=ROOT, log=print):
    """Fetch only missing files. Refuses to replace files that exist with the wrong hash."""
    for group in manifest['groups']:
        dest = resolve_dest(group['dest'], root)
        missing = []
        for entry in group['files']:
            problem = check_file(dest / entry['path'], entry)
            if problem == 'missing':
                missing.append(entry)
            elif problem:
                raise AssetError(f'{dest / entry["path"]}: {problem}. Not overwriting; delete it and run again.')
        if not missing:
            log(f"{group['name']}: ok")
            continue
        log(f"{group['name']}: fetching {len(missing)} file(s) from {group.get('source', 'pinned source')}")
        if 'archive' in group:
            extract_archive(group, dest, missing, log)
        else:
            for entry in missing:
                log(f"  {entry['path']}")
                download(entry['url'], dest / entry['path'], entry['sha256'], entry.get('size'))


def hardware_report():
    """Return [(ok, line)]. Opens no audio stream and records nothing."""
    lines = [(True, f'{platform.system()} {platform.machine()}, Python {platform.python_version()}')]
    if sys.version_info[:2] != (3, 11):
        lines.append((False, 'voice/README.md targets Python 3.11'))
    for name in PACKAGES:
        found = importlib.util.find_spec(name) is not None
        lines.append((found, f"package {name}: {'found' if found else 'missing (install voice/requirements.txt)'}"))
    try:
        import sounddevice as sd
    except Exception as exc:  # PortAudio missing raises OSError on import
        lines.append((False, f'sounddevice unusable: {exc}. On Linux install libportaudio2.'))
        return lines
    device = os.environ.get('EDDIE_INPUT_DEVICE')
    if device and device.isdigit():
        device = int(device)
    try:
        info = sd.query_devices(device, 'input')
        sd.check_input_settings(device=device, samplerate=16000, channels=1, dtype='int16')
        lines.append((True, f"input: {info['name']} accepts 16 kHz mono int16"))
    except Exception as exc:
        lines.append((False, f'input device {device or "default"} unusable for 16 kHz mono: {exc}'))
    try:
        info = sd.query_devices(None, 'output')
        lines.append((True, f"output: {info['name']}"))
    except Exception as exc:
        lines.append((False, f'no output device: {exc}'))
    piper = os.environ.get('EDDIE_PIPER_MODEL')
    if piper and not (Path(piper).is_file() and Path(piper + '.json').is_file()):
        lines.append((False, f'EDDIE_PIPER_MODEL={piper} or its .json companion is missing'))
    elif not piper and platform.system() != 'Darwin':
        lines.append((False, 'EDDIE_PIPER_MODEL unset; Linux has no say fallback'))
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify', action='store_true', help='hash assets only; never uses the network')
    mode.add_argument('--hardware', action='store_true', help='check packages and audio devices only')
    parser.add_argument('--manifest', type=Path, default=MANIFEST)
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text())

    if not args.verify and not args.hardware:
        try:
            provision(manifest)
        except (AssetError, OSError) as exc:
            print(f'PROVISIONING FAILED: {exc}', file=sys.stderr)
            return 1
    if not args.hardware:
        problems = verify(manifest)
        for group, path, problem in problems:
            print(f'ASSET FAILED [{group}] {path}: {problem}', file=sys.stderr)
        if problems:
            print('Voice assets are not ready. Run python scripts/provision_voice.py while online.', file=sys.stderr)
            return 1
        print(f"Verified {sum(len(g['files']) for g in manifest['groups'])} voice assets.")
    if args.verify:
        return 0
    report = hardware_report()
    for ok, line in report:
        print(f"{'ok  ' if ok else 'FAIL'} {line}")
    return 0 if all(ok for ok, _ in report) else 1


if __name__ == '__main__':
    sys.exit(main())
