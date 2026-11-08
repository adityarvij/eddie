import http.server
import json
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from eddie import classifier, decision, dns_server, store
from eddie.decision import Verdict

TV_IP = '192.168.1.20'
TV_MAC = 'f0:4f:7c:11:22:33'
GOOD = '{"label": "tracker", "confidence": 0.9, "reason": "Fire TV usage telemetry"}'


def now_ms():
    return int(time.time() * 1000)


class ParseVerdictTests(unittest.TestCase):
    def test_accepts_the_contract(self):
        self.assertEqual(Verdict('tracker', 0.9, 'Fire TV usage telemetry'), classifier.parse_verdict(GOOD))
        self.assertEqual(Verdict('needed', 1.0, 'video'),
                         classifier.parse_verdict('{"label": "needed", "confidence": 1, "reason": " video\\n"}'))

    def test_rejects_anything_else(self):
        bad = [
            None, '', 'not json', '[]', '"tracker"',
            '{"label": "tracker", "confidence": 0.9}',
            '{"label": "tracker", "confidence": 0.9, "reason": "x", "extra": 1}',
            '{"label": "malware", "confidence": 0.9, "reason": "x"}',
            '{"label": "tracker", "confidence": "0.9", "reason": "x"}',
            '{"label": "tracker", "confidence": true, "reason": "x"}',
            '{"label": "tracker", "confidence": 1.5, "reason": "x"}',
            '{"label": "tracker", "confidence": -0.1, "reason": "x"}',
            '{"label": "tracker", "confidence": NaN, "reason": "x"}',
            '{"label": "tracker", "confidence": 0.9, "reason": ""}',
            '{"label": "tracker", "confidence": 0.9, "reason": 5}',
            '{"label": "tracker", "confidence": 0.9, "reason": "' + ' '.join(['word'] * 15) + '"}',
            '{"label": "tracker", "confidence": 0.9, "reason": "' + 'x' * 121 + '"}',
            '{"label": "tracker", "confidence": 0.9, "reason": "bell \\u0007 here"}',
        ]
        for text in bad:
            self.assertIsNone(classifier.parse_verdict(text), text)


class CheckLocalTests(unittest.TestCase):
    def test_allows_localhost_and_private_ip_literals(self):
        for url in ('http://127.0.0.1:11434', 'http://localhost:11434', 'http://[::1]:11434',
                    'http://192.168.1.10:11434', 'http://10.0.0.5:11434', 'http://172.20.10.2:11434'):
            classifier.check_local(url)

    def test_refuses_public_hosts_and_hostnames(self):
        for url in ('http://8.8.8.8:11434', 'https://api.example.com', 'http://mac.local:11434',
                    'http://0.0.0.0:11434', 'ftp://127.0.0.1', '127.0.0.1:11434', 'http://100.64.0.1'):
            with self.assertRaises(ValueError, msg=url):
                classifier.check_local(url)


class DBTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'eddie.db')
        self.conn = store.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def query(self, domain, ago_ms=1000, device=TV_IP, source='default', action='flag'):
        with self.conn:
            self.conn.execute('INSERT INTO queries (ts, device_mac, domain, qtype, action, source, reason)'
                              ' VALUES (?, ?, ?, ?, ?, ?, ?)',
                              (now_ms() - ago_ms, device, domain, 'A', action, source, ''))

    def verdicts(self):
        return self.conn.execute('SELECT domain, label, confidence, reason FROM ai_verdicts ORDER BY domain').fetchall()


class QueueTests(DBTestCase):
    def test_candidates_are_recent_unknowns_without_a_verdict(self):
        self.query('new.example')
        self.query('newest.example', ago_ms=10)
        self.query('known.example')
        self.query('blocked.example', source='blocklist', action='block')
        self.query('stale.example', ago_ms=classifier.LOOKBACK_MS + 1000)
        with self.conn:
            self.conn.execute("INSERT INTO ai_verdicts VALUES ('known.example', 'needed', 0.9, 'x', ?)", (now_ms(),))
        self.assertEqual(['new.example', 'newest.example'], classifier.candidates(self.conn, now_ms(), 10))

    def test_network_plumbing_is_not_queued(self):
        for domain in ('54.25.110.10.in-addr.arpa', '_dns.resolver.arpa', 'b._dns-sd._udp.neu.edu',
                       '_ipp._tcp.printers.example.edu', 'printer.local', 'wpad', 'my_host.example.com'):
            self.query(domain)
        self.assertEqual(['my_host.example.com'], classifier.candidates(self.conn, now_ms(), 10))

    def test_facts_carry_only_domain_label_counts_and_idle(self):
        for ago in (1000, 2000, classifier.COUNT_WINDOW_MS + 1000):
            self.query('t.example', ago_ms=ago, device=TV_MAC)
        self.query('t.example', device='192.168.1.99')
        with self.conn:
            self.conn.execute("INSERT INTO devices (mac, ip, label) VALUES (?, ?, 'Fire Stick')", (TV_MAC, TV_IP))
        question = classifier.facts(self.conn, 't.example', now_ms())
        self.assertEqual({'domain': 't.example', 'device': 'Fire Stick', 'queries_10min': 3, 'idle': None}, question)

    def test_unlabelled_device_is_never_sent_as_ip_or_mac(self):
        self.query('t.example')
        question = classifier.facts(self.conn, 't.example', now_ms())
        self.assertIsNone(question['device'])
        self.assertNotIn(TV_IP, json.dumps(question))

    def test_idle_from_recent_allowlisted_traffic(self):
        self.query('t.example')
        self.query('netflix.com', ago_ms=classifier.IDLE_WINDOW_MS + 1000, source='allowlist', action='allow')
        self.assertTrue(classifier.facts(self.conn, 't.example', now_ms())['idle'])
        self.query('netflix.com', source='allowlist', action='allow')
        self.assertFalse(classifier.facts(self.conn, 't.example', now_ms())['idle'])

    def test_essentials_are_not_mistaken_for_media_activity(self):
        self.query('t.example')
        self.query('netflix.com',ago_ms=classifier.IDLE_WINDOW_MS+1000,source='allowlist',action='allow')
        self.query('pool.ntp.org',source='allowlist',action='allow')
        self.assertTrue(classifier.facts(self.conn,'t.example',now_ms())['idle'])



class ClassifierTests(DBTestCase):
    def test_stores_a_valid_verdict_once(self):
        self.query('t.example')
        asked = []
        worker = classifier.Classifier(self.conn, lambda q: asked.append(q) or GOOD)
        self.assertEqual(1, worker.run_once())
        self.assertEqual(0, worker.run_once())
        self.assertEqual([('t.example', 'tracker', 0.9, 'Fire TV usage telemetry')], self.verdicts())
        self.assertEqual(['t.example'], [q['domain'] for q in asked])

    def test_malformed_output_is_not_stored_and_backs_off(self):
        self.query('t.example')
        calls = []
        worker = classifier.Classifier(self.conn, lambda q: calls.append(q) or 'sure, it is a tracker')
        with self.assertLogs('eddie.classifier', 'WARNING') as logs:
            self.assertEqual(0, worker.run_once())
        self.assertEqual(0, worker.run_once())
        self.assertEqual([], self.verdicts())
        self.assertEqual(1, len(calls))
        self.assertNotIn('t.example', '\n'.join(logs.output))

    def test_backed_off_domain_doesnt_starve_others(self):
        self.query('bad.example', ago_ms=30)
        self.query('good.example', ago_ms=20)
        worker = classifier.Classifier(self.conn, lambda q: GOOD if q['domain'] == 'good.example' else 'nope')
        with self.assertLogs('eddie.classifier', 'WARNING'):
            worker.run_once(limit=1)
        self.assertEqual(1, worker.run_once(limit=1))

    def test_odd_domain_names_are_never_sent(self):
        self.query('bad domain\n.example')
        worker = classifier.Classifier(self.conn, lambda q: self.fail('sent to model'))
        self.assertEqual(0, worker.run_once())

    def test_unreachable_model_raises_for_the_caller_to_wait(self):
        self.query('t.example')

        def down(q):
            raise ConnectionRefusedError()
        with self.assertRaises(OSError):
            classifier.Classifier(self.conn, down).run_once()

    def test_dns_engine_uses_the_verdict_after_reload(self):
        engine = decision.Engine()
        watcher = dns_server.StoreWatcher(self.path, engine)
        watcher.poll()
        self.assertEqual('default', engine.evaluate('t.example', TV_IP).source)
        self.query('t.example')
        classifier.Classifier(self.conn, lambda q: GOOD).run_once()
        self.assertTrue(watcher.poll())
        self.assertFalse(watcher.poll())
        self.assertEqual(('flag', 'ai'), (engine.evaluate('t.example', TV_IP).action,
                                          engine.evaluate('t.example', TV_IP).source))  # AI never blocks alone
        watcher.close()


class FakeAPI(http.server.BaseHTTPRequestHandler):
    status = 200
    seen = []

    def do_POST(self):
        FakeAPI.seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
        self.send_response(self.status)
        if self.status == 302:
            self.send_header('Location', 'http://8.8.8.8/ai/verdict')
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


class AskModelTests(unittest.TestCase):
    def test_answers_from_the_local_model_with_contract_json(self):
        question = {'domain': 'adsync.brightnest-tv.com', 'device': 'TV', 'queries_10min': 2, 'idle': True}
        verdict = classifier.parse_verdict(classifier.ask_model(question))
        self.assertEqual('tracker', verdict.label)

    def test_classifier_no_longer_talks_to_ollama(self):
        source = Path(classifier.__file__).read_text()
        for gone in ('ask_ollama', 'EDDIE_OLLAMA_URL', '11434', '/api/chat'):
            self.assertNotIn(gone, source)


class PublishVerdictTests(unittest.TestCase):
    def setUp(self):
        FakeAPI.seen, FakeAPI.status = [], 200
        self.server = http.server.HTTPServer(('127.0.0.1', 0), FakeAPI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = unittest.mock.patch.dict('os.environ', {'EDDIE_API_URL': f'http://127.0.0.1:{self.server.server_port}'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_posts_the_verdict_to_the_local_api(self):
        event = {'domain': 't.example', 'label': 'tracker', 'confidence': 0.9, 'reason': 'x', 'ts': 1}
        classifier.publish_verdict(event)
        self.assertEqual([('/ai/verdict', event)], FakeAPI.seen)

    def test_does_not_follow_redirects(self):
        FakeAPI.status = 302
        with self.assertRaises(OSError):
            classifier.publish_verdict({'domain': 't.example'})
        self.assertEqual(1, len(FakeAPI.seen))


class RetentionTests(DBTestCase):
    def test_prune_removes_old_verdicts_too(self):
        now = now_ms()
        with self.conn:
            self.conn.executemany('INSERT INTO ai_verdicts VALUES (?, ?, ?, ?, ?)', [
                ('old.example', 'tracker', 0.9, 'x', now - 8 * 86_400_000),
                ('new.example', 'tracker', 0.9, 'x', now)])
        self.assertEqual(1, store.prune(self.conn, 7, now_ms=now))
        self.assertEqual(['new.example'], [r[0] for r in self.verdicts()])


if __name__ == '__main__':
    unittest.main()
