import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from eddie import decision, lists


class ParseTests(unittest.TestCase):
    def test_hosts_and_plain_formats(self):
        lines = [
            '# Title: some list',
            '0.0.0.0 Ads.Example.COM',
            '127.0.0.1 tracker.example  # inline comment',
            '0.0.0.0 a.example b.example',
            'plain.example.',
            '   ',
            '# commented.example',
        ]
        self.assertEqual({'ads.example.com', 'tracker.example', 'a.example', 'b.example', 'plain.example'},
                         lists.parse(lines))

    def test_skips_hosts_file_boilerplate_and_junk(self):
        lines = ['127.0.0.1 localhost', '127.0.0.1 localhost.localdomain', '127.0.0.1 local',
                 '255.255.255.255 broadcasthost', '::1 ip6-localhost', 'fe80::1%lo0 localhost',
                 'ff02::1 ip6-allnodes', '0.0.0.0 0.0.0.0', '0.0.0.0 1.2.3.4', 'not a domain!',
                 'singlelabel', 'bad..example']
        self.assertEqual(set(), lists.parse(lines))


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / 'blocks').mkdir()
        (self.dir / 'blocks' / 'one.txt').write_text('0.0.0.0 one.example\n')
        (self.dir / 'blocks' / 'two.txt').write_text('two.example\n')
        (self.dir / 'blocks' / 'companies.json').write_text('{"not": "a list"}')
        (self.dir / 'allow.txt').write_text('# essentials\nok.example\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_every_txt_list_and_the_allowlist(self):
        self.assertEqual(({'ok.example'}, {'one.example', 'two.example'}),
                         lists.load(self.dir / 'blocks', self.dir / 'allow.txt'))

    def test_missing_allowlist_is_an_error(self):
        with self.assertRaises(OSError):
            lists.load(self.dir / 'blocks', self.dir / 'missing.txt')

    def test_env_paths_and_off(self):
        env = {'EDDIE_BLOCKLISTS': str(self.dir / 'blocks'), 'EDDIE_ALLOWLIST': str(self.dir / 'allow.txt')}
        with patch.dict('os.environ', env):
            self.assertEqual({'one.example', 'two.example'}, lists.load_from_env()[1])
        with patch.dict('os.environ', {**env, 'EDDIE_BLOCKLISTS': 'off'}):
            self.assertEqual(({'ok.example'}, set()), lists.load_from_env())


class ShippedListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        started = time.monotonic()
        cls.allow, cls.block = lists.load()
        cls.load_seconds = time.monotonic() - started
        cls.engine = decision.Engine()
        cls.engine.set_lists(cls.allow, cls.block)

    def test_loads_in_under_two_seconds(self):
        self.assertGreater(len(self.block), 50_000)
        self.assertLess(self.load_seconds, 2.0)

    def test_every_allowlist_line_is_a_domain(self):
        lines = [l.split('#')[0].strip() for l in lists.ALLOWLIST.read_text().splitlines()]
        self.assertEqual(len([l for l in lines if l]), len(self.allow))

    def test_allowlist_unblocks_nothing_on_the_blocklists(self):
        unblocked = {b for b in self.block if decision.matches(b, self.allow)}
        self.assertEqual(set(), unblocked)

    def test_decisions_with_the_shipped_lists(self):
        cases = {
            'device-metrics-us.amazon.com': ('block', 'blocklist'),
            'logs.netflix.com': ('block', 'blocklist'),
            'mads.amazon.com': ('block', 'blocklist'),
            'www.netflix.com': ('allow', 'allowlist'),
            'ipv4-c001-sea001-ix.1.oca.nflxvideo.net': ('allow', 'allowlist'),
            'rr3---sn-abc.googlevideo.com': ('allow', 'allowlist'),
            'api.amazon.com': ('allow', 'allowlist'),
            'connectivitycheck.gstatic.com': ('allow', 'allowlist'),
            'fireoscaptiveportal.com': ('allow', 'allowlist'),
            'time.apple.com': ('allow', 'allowlist'),
            # Shared infrastructure the local model once called trackers.
            'www.gstatic.com': ('allow', 'allowlist'),
            'encrypted-tbn0.gstatic.com': ('allow', 'allowlist'),
            'lh3.googleusercontent.com': ('allow', 'allowlist'),
            'mesu.g.aaplimg.com': ('allow', 'allowlist'),
            'google-ohttp-relay-safebrowsing.fastly-edge.com': ('allow', 'allowlist'),
            # Their tracking neighbours stay blocked.
            'csi.gstatic.com': ('block', 'blocklist'),
            'displayads-formats.googleusercontent.com': ('block', 'blocklist'),
            # Eddie's extra list (blocklists/eddie-extra.txt).
            'collector.github.com': ('block', 'blocklist'),
            'ab.chatgpt.com': ('block', 'blocklist'),
            'ep1.adtrafficquality.google': ('block', 'blocklist'),
            'http-inputs-notion.splunkcloud.com': ('block', 'blocklist'),
            'a.nel.cloudflare.com': ('block', 'blocklist'),
            # Their apps stay reachable.
            'github.com': ('flag', 'default'),
            'chatgpt.com': ('flag', 'default'),
        }
        for domain, expected in cases.items():
            d = self.engine.evaluate(domain, 'tv')
            self.assertEqual(expected, (d.action, d.source), domain)


if __name__ == '__main__':
    unittest.main()
