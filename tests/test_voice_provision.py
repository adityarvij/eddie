import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import provision_voice as pv

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Provisioning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / 'upstream'
        self.src.mkdir()
        self.model = b'weights' * 100
        (self.src / 'model.bin').write_bytes(self.model)
        with zipfile.ZipFile(self.src / 'bundle.zip', 'w') as zf:
            zf.writestr('bundle/a.txt', b'alpha')
            zf.writestr('../escape.txt', b'evil')
        self.zip_sha = sha((self.src / 'bundle.zip').read_bytes())

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, model_sha=None, zip_sha=None):
        return {'groups': [
            {'name': 'plain', 'dest': 'models/plain', 'files': [
                {'path': 'model.bin', 'size': len(self.model), 'sha256': model_sha or sha(self.model),
                 'url': (self.src / 'model.bin').as_uri()}]},
            {'name': 'zipped', 'dest': 'models', 'archive': {'url': (self.src / 'bundle.zip').as_uri(),
                                                              'sha256': zip_sha or self.zip_sha},
             'files': [{'path': 'bundle/a.txt', 'sha256': sha(b'alpha')}]},
        ]}

    def test_fresh_provision_then_offline_verify(self):
        manifest = self.manifest()
        pv.provision(manifest, root=self.root, log=lambda *_: None)
        self.assertEqual((self.root / 'models/plain/model.bin').read_bytes(), self.model)
        self.assertFalse((self.root / 'escape.txt').exists())
        self.assertFalse((self.root.parent / 'escape.txt').exists())
        with patch('urllib.request.urlopen', side_effect=AssertionError('network used')):
            self.assertEqual(pv.verify(manifest, root=self.root), [])
            pv.provision(manifest, root=self.root, log=lambda *_: None)  # already present: no fetch

    def test_download_hash_mismatch_fails_and_leaves_nothing(self):
        with self.assertRaisesRegex(pv.AssetError, 'SHA-256 mismatch'):
            pv.provision(self.manifest(model_sha='0' * 64), root=self.root, log=lambda *_: None)
        self.assertEqual(list((self.root / 'models/plain').iterdir()), [])

    def test_archive_hash_mismatch_extracts_nothing(self):
        with self.assertRaisesRegex(pv.AssetError, 'SHA-256 mismatch'):
            pv.provision(self.manifest(zip_sha='0' * 64), root=self.root, log=lambda *_: None)
        self.assertFalse((self.root / 'models/bundle').exists())

    def test_tampered_asset_is_reported_and_not_overwritten(self):
        manifest = self.manifest()
        pv.provision(manifest, root=self.root, log=lambda *_: None)
        target = self.root / 'models/plain/model.bin'
        target.write_bytes(b'tampered')
        problems = pv.verify(manifest, root=self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn('mismatch', problems[0][2])
        with self.assertRaisesRegex(pv.AssetError, 'Not overwriting'):
            pv.provision(manifest, root=self.root, log=lambda *_: None)
        self.assertEqual(target.read_bytes(), b'tampered')

    def test_verify_cli_exits_nonzero_without_network(self):
        path = self.root / 'manifest.json'
        path.write_text(json.dumps(self.manifest()))
        err = io.StringIO()
        with patch.object(pv, 'ROOT', self.root), patch('urllib.request.urlopen', side_effect=AssertionError('network used')), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(pv.main(['--verify', '--manifest', str(path)]), 1)
        self.assertIn('ASSET FAILED [plain]', err.getvalue())
        self.assertIn('missing', err.getvalue())

    def test_shipped_manifest_pins_every_download(self):
        manifest = json.loads((ROOT / 'voice/assets.json').read_text())
        self.assertEqual({g['name'] for g in manifest['groups']}, {'whisper', 'piper', 'vosk', 'openwakeword', 'intents'})
        for group in manifest['groups']:
            sources = [group['archive']] if 'archive' in group else group['files']
            for item in sources + group['files']:
                self.assertRegex(item['sha256'], '^[0-9a-f]{64}$')
            for item in sources:
                self.assertTrue(item['url'].startswith('https://'))
                self.assertNotIn('/resolve/main/', item['url'])


if __name__ == '__main__':
    unittest.main()
