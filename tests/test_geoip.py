import asyncio
import gzip
import hashlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from dnslib import AAAA, CNAME, QTYPE, RR, A, DNSRecord

from eddie import api, dns_server, geoip
from eddie.geoip import CountryDB, Destinations, GeoIPError, GeoWatch
from tests.test_dns_server import BLOCKED, REAL_IP, ServerCase

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests' / 'fixtures' / 'geoip-country.csv'
FIXTURE_SHA = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
NAMES = {'US': 'the United States', 'RU': 'Russia', 'CH': 'Switzerland', 'DE': 'Germany'}
DOORBELL = 'aa:bb:cc:dd:ee:10'

sys.path.insert(0, str(ROOT / 'scripts'))
import provision_geoip  # noqa: E402


def answer(name, *ips, cname=None):
    """An upstream reply: optional CNAME, then an A or AAAA per address."""
    reply = DNSRecord.question(name).reply()
    owner = name
    if cname:
        reply.add_answer(RR(name, QTYPE.CNAME, rdata=CNAME(cname)))
        owner = cname
    for ip in ips:
        reply.add_answer(RR(owner, QTYPE.AAAA, rdata=AAAA(ip)) if ':' in ip else RR(owner, QTYPE.A, rdata=A(ip)))
    return reply.pack()


def learned(**kw):
    """Destinations that stop learning after 2 answers and 1 s, seeded with the US."""
    d = Destinations(NAMES, min_answers=2, learn_ms=1000, **kw)
    for ts in (0, 500, 1000):
        d.observe(ts, DOORBELL, 'Doorbell', 'ring.com', [('93.184.216.34', 'US')])
    return d


class CountryDBTests(unittest.TestCase):
    def setUp(self):
        self.db = CountryDB.load(FIXTURE)

    def test_ipv4_ranges(self):
        self.assertEqual('RU', self.db.country('5.6.7.8'))
        self.assertEqual('US', self.db.country('93.184.216.34'))
        self.assertEqual('US', self.db.country('93.184.217.0'), 'codes are upper-cased')
        self.assertEqual('CH', self.db.country('9.9.9.255'))

    def test_gaps_reserved_and_outside_are_unknown(self):
        for ip in ('5.6.6.255', '5.6.8.0', '0.0.0.1', '255.255.255.255', 'not an ip', '2a00:1451::1', '::1'):
            self.assertIsNone(self.db.country(ip), ip)

    def test_ipv6_ranges_including_one_not_on_a_64_boundary(self):
        self.assertEqual('US', self.db.country('2001:4860:4860::8888'))
        self.assertEqual('DE', self.db.country('2a00:1450::ffff'))
        self.assertIsNone(self.db.country('2a00:1450::1:0'), 'ZZ starts mid-/64')

    def test_gzip_loads_the_same(self):
        with tempfile.TemporaryDirectory() as tmp:
            gz = Path(tmp) / 'db.csv.gz'
            gz.write_bytes(gzip.compress(FIXTURE.read_bytes()))
            self.assertEqual('RU', CountryDB.load(gz).country('5.6.7.8'))

    def test_unsorted_overlapping_or_broken_rows_are_refused(self):
        for rows in ('5.6.7.0,5.6.7.255,RU\n1.0.0.0,1.0.0.255,AU\n',
                     '5.6.7.0,5.6.7.255,RU\n5.6.7.128,5.6.8.0,US\n',
                     '5.6.7.9,5.6.7.0,RU\n',
                     '5.6.7.0,RU\n',
                     'start,end,country\n'):
            with tempfile.NamedTemporaryFile('w', suffix='.csv') as f:
                f.write(rows)
                f.flush()
                with self.assertRaises(GeoIPError, msg=rows):
                    CountryDB.load(f.name)

    def test_pinned_hash_is_required(self):
        self.assertEqual('RU', geoip.load_verified(FIXTURE, FIXTURE_SHA).country('5.6.7.8'))
        with self.assertRaisesRegex(GeoIPError, 'expected the pinned'):
            geoip.load_verified(FIXTURE, '0' * 64)
        with self.assertRaisesRegex(GeoIPError, 'provision_geoip'):
            geoip.load_verified(ROOT / 'nope.csv', FIXTURE_SHA)

    def test_repo_pin_and_country_names(self):
        pin = geoip.manifest()
        self.assertRegex(pin['sha256'], '^[0-9a-f]{64}$')
        self.assertTrue(pin['url'].startswith('https://'))
        self.assertIn('CC BY 4.0', pin['license'])
        names = geoip.country_names()
        self.assertEqual(249, len(names))
        self.assertEqual(('the United States', 'Russia'), (names['US'], names['RU']))


class AnswerTests(unittest.TestCase):
    def test_public_a_and_aaaa_after_a_cname(self):
        packet = answer('cam.example', '5.6.7.8', '2001:4860::1', '5.6.7.8', cname='edge.cdn.example')
        self.assertEqual(['5.6.7.8', '2001:4860::1'], geoip.answer_ips(packet))

    def test_private_sinkholed_and_garbage_are_skipped(self):
        self.assertEqual([], geoip.answer_ips(answer('nas.example', '192.168.1.5', '0.0.0.0', '::', '10.0.0.1')))
        self.assertEqual([], geoip.answer_ips(b'\x00garbage'))


class DestinationsTests(unittest.TestCase):
    def test_learning_is_silent(self):
        d = Destinations(NAMES, min_answers=2, learn_ms=1000)
        self.assertEqual([], d.observe(0, DOORBELL, 'Doorbell', 'ring.com', [('93.184.216.34', 'US')]))
        self.assertEqual([], d.observe(10, DOORBELL, 'Doorbell', 'x.example', [('5.6.7.8', 'RU')]))

    def test_needs_both_answers_and_time(self):
        d = Destinations(NAMES, min_answers=2, learn_ms=1000)
        for ts in (0, 1, 2, 3):  # plenty of answers, not enough time
            d.observe(ts, DOORBELL, 'Doorbell', 'ring.com', [('93.184.216.34', 'US')])
        self.assertEqual([], d.observe(4, DOORBELL, 'Doorbell', 'x.example', [('5.6.7.8', 'RU')]))
        d = Destinations(NAMES, min_answers=2, learn_ms=1000)
        d.observe(0, DOORBELL, 'Doorbell', 'ring.com', [('93.184.216.34', 'US')])
        self.assertEqual([], d.observe(5000, DOORBELL, 'Doorbell', 'x.example', [('5.6.7.8', 'RU')]),
                         'enough time, too few answers')

    def test_new_country_names_device_domain_country_and_caveat(self):
        [alert] = learned().observe(2000, DOORBELL, 'Doorbell', 'telemetry.example', [('5.6.7.8', 'RU')])
        self.assertEqual({'type': 'geo_alert', 'ts': 2000, 'device_mac': DOORBELL, 'device': 'Doorbell',
                          'domain': 'telemetry.example', 'ip': '5.6.7.8', 'country': 'RU', 'country_name': 'Russia',
                          'previous': ['US'], 'approximate': True},
                         {k: v for k, v in alert.items() if k != 'reason'})
        self.assertTrue(alert['reason'].startswith(
            'Doorbell started talking to a server in Russia (telemetry.example, 5.6.7.8). '
            'Until now its servers were in the United States.'))
        self.assertIn('approximate', alert['reason'])
        self.assertIn('CDNs', alert['reason'])
        api.GeoAlertEvent(**alert)  # the API takes exactly what the DNS side sends

    def test_known_countries_and_repeats_dont_alert(self):
        d = learned()
        self.assertEqual([], d.observe(2000, DOORBELL, 'Doorbell', 'ring.com', [('93.184.216.1', 'US')]))
        self.assertEqual(1, len(d.observe(2001, DOORBELL, 'Doorbell', 'a.example', [('5.6.7.8', 'RU')])))
        self.assertEqual([], d.observe(2002, DOORBELL, 'Doorbell', 'b.example', [('5.6.7.9', 'RU')]))

    def test_each_device_has_its_own_countries(self):
        d = learned()
        self.assertEqual([], d.observe(2000, 'ip:192.168.1.9', '192.168.1.9', 'a.example', [('5.6.7.8', 'RU')]),
                         'a new device is learning')

    def test_a_roaming_device_goes_quiet(self):
        d = learned(unstable_new=2, unstable_ms=10_000)
        sent = [d.observe(2000 + i, DOORBELL, 'Doorbell', 'a.example', [(f'5.6.7.{i}', code)])
                for i, code in enumerate(('RU', 'CH', 'DE', 'FR'))]
        self.assertEqual([1, 1, 0, 0], [len(s) for s in sent])

    def test_memory_is_bounded(self):
        d = Destinations(NAMES, max_devices=3)
        for i in range(10):
            d.observe(i, f'ip:10.0.0.{i}', '10.0.0.x', 'a.example', [('5.6.7.8', 'RU')])
        self.assertEqual(['ip:10.0.0.7', 'ip:10.0.0.8', 'ip:10.0.0.9'], list(d.devices))


class GeoWatchTests(unittest.TestCase):
    def watch(self, sha=FIXTURE_SHA, **kw):
        sent = []
        w = GeoWatch(FIXTURE, sha, sent.extend, destinations=learned(), **kw)
        self.addCleanup(w.close)
        self.assertTrue(w.ready.wait(5))
        return w, sent

    def test_alerts_from_raw_answers_off_the_callers_thread(self):
        w, sent = self.watch()
        w.observe(2000, DOORBELL, 'Doorbell', 'ring.com', answer('ring.com', '93.184.216.34'))
        w.observe(2001, DOORBELL, 'Doorbell', 'cam.example', answer('cam.example', '192.168.1.2', '5.6.7.8'))
        w.close()
        self.assertEqual([('cam.example', 'RU', '5.6.7.8')], [(a['domain'], a['country'], a['ip']) for a in sent])

    def test_wrong_hash_turns_it_off(self):
        with self.assertLogs('eddie.geoip', 'WARNING') as logs:
            w, sent = self.watch(sha='0' * 64)
        self.assertTrue(w.off)
        self.assertIn('geo alerts off', logs.output[0])
        w.observe(2000, DOORBELL, 'Doorbell', 'cam.example', answer('cam.example', '5.6.7.8'))
        self.assertTrue(w.pending.empty())
        w.close()
        self.assertEqual([], sent)

    def test_observe_never_blocks_when_the_worker_is_stuck(self):
        gate = threading.Event()
        w = GeoWatch(FIXTURE, FIXTURE_SHA, lambda alerts: gate.wait(5), destinations=learned(), max_pending=2)
        self.assertTrue(w.ready.wait(5))
        for i in range(50):
            w.observe(2000 + i, DOORBELL, 'Doorbell', f'{i}.example', answer('a.example', '5.6.7.8'))
        self.assertGreater(w.dropped, 0)
        gate.set()
        w.close()

    def test_env(self):
        with patch.dict('os.environ', {'EDDIE_GEOIP': 'off'}):
            self.assertIsNone(geoip.geo_from_env(print))
        with patch.dict('os.environ', {'EDDIE_GEOIP': str(FIXTURE)}), self.assertLogs('eddie.geoip', 'WARNING'):
            w = geoip.geo_from_env(print)  # the fixture isn't the pinned database
            self.assertTrue(w.ready.wait(5))
            self.assertTrue(w.off)
            w.close()


class RecordingWatch:
    def __init__(self):
        self.seen = []

    def observe(self, *args):
        self.seen.append(args)


class DNSHookTests(ServerCase):
    """resolve hands forwarded answers to the watch and nothing else."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.recorder = RecordingWatch()
        patcher = patch.dict(dns_server.geo, {'watch': self.recorder})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_forwarded_answer_is_queued_with_device_and_domain(self):
        conn = await self.start_server([self.upstream_addr])
        await self.ask(conn, 'Cam.Example.')
        [(ts, device_mac, device, domain, packet)] = self.recorder.seen
        self.assertEqual(('ip:127.0.0.1', '127.0.0.1', 'cam.example'), (device_mac, device, domain))
        self.assertEqual([REAL_IP], geoip.answer_ips(packet))

    async def test_blocked_and_offline_answers_are_not(self):
        conn = await self.start_server([self.upstream_addr])
        await self.ask(conn, BLOCKED)
        await self.ask(conn, 'cam.example')
        with patch.object(dns_server.offline_mode, 'on', True):
            await self.ask(conn, 'cam.example')
        self.assertEqual(1, len(self.recorder.seen))


class APITests(unittest.TestCase):
    def test_alerts_are_broadcast_kept_for_the_snapshot_and_not_lookups(self):
        from fastapi.testclient import TestClient
        [alert] = learned().observe(2000, DOORBELL, 'Doorbell', 'a.example', [('5.6.7.8', 'RU')])
        sent = []

        async def broadcast(msg):
            sent.append(msg)

        with patch.object(api, 'geo_recent', []) as kept, patch.object(api, 'broadcast', broadcast), \
                patch.object(api, 'totals', {'seen': 0, 'blocked': 0}), patch.dict('os.environ', {'EDDIE_DB': 'off'}):
            client = TestClient(api.app, client=('127.0.0.1', 50000))
            self.assertEqual({'ok': True, 'count': 1}, client.post('/geo/alerts', json=[alert]).json())
            self.assertEqual(422, client.post('/geo/alerts', json={**alert, 'approximate': False}).status_code)
            self.assertEqual(422, client.post('/geo/alerts', json={**alert, 'country': 'Russia'}).status_code)
            with client.websocket_connect('/ws/events') as ws:
                snapshot = json.loads(ws.receive_text())
        self.assertEqual([alert], sent)
        self.assertEqual([alert], kept)
        self.assertEqual([alert], snapshot['geo'])
        self.assertEqual(0, snapshot['totals']['seen'])


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.pin = {'path': 'data/geoip/db.csv', 'url': 'https://example.invalid/db.csv', 'size': FIXTURE.stat().st_size,
                    'sha256': FIXTURE_SHA, 'source': 'fixture', 'license': 'test'}

    def test_copies_a_local_file_only_when_its_hash_is_pinned(self):
        target = provision_geoip.provision(self.pin, self.root, source=FIXTURE, log=lambda _: None)
        self.assertEqual(FIXTURE.read_bytes(), target.read_bytes())
        other = self.root / 'other.csv'
        other.write_text('1.0.0.0,1.0.0.255,AU\n')
        target.unlink()
        with self.assertRaisesRegex(provision_geoip.AssetError, 'SHA-256 mismatch'):
            provision_geoip.provision(self.pin, self.root, source=other, log=lambda _: None)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name('db.csv.part').exists())

    def test_never_overwrites_an_altered_file_or_downloads_when_present(self):
        target = self.root / self.pin['path']
        target.parent.mkdir(parents=True)
        target.write_text('tampered')
        with patch.object(provision_geoip, 'download') as download:
            with self.assertRaisesRegex(provision_geoip.AssetError, 'Not overwriting'):
                provision_geoip.provision(self.pin, self.root, log=lambda _: None)
            shutil.copyfile(FIXTURE, target)
            provision_geoip.provision(self.pin, self.root, log=lambda _: None)
        download.assert_not_called()

    def test_downloads_the_pinned_url_when_missing(self):
        with patch.object(provision_geoip, 'download') as download:
            provision_geoip.provision(self.pin, self.root, log=lambda _: None)
        download.assert_called_once_with(self.pin['url'], self.root / self.pin['path'], FIXTURE_SHA, self.pin['size'])

    def test_verify_never_uses_the_network(self):
        manifest = self.root / 'geoip.json'
        manifest.write_text(json.dumps({**self.pin, 'path': str(self.root / 'missing.csv')}))
        err = io.StringIO()
        with patch.object(provision_geoip, 'download') as download, redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(1, provision_geoip.main(['--verify', '--manifest', str(manifest)]))
        download.assert_not_called()
        self.assertIn('missing', err.getvalue())


if __name__ == '__main__':
    unittest.main()
