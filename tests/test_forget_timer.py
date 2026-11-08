"""Health, beliefs and kids lookups are summarised, then deleted, after 24 hours (#111)."""
import sqlite3
import tempfile
import time
import unittest
from collections import deque
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from eddie import api, store

HOUR = 3_600_000
DAY = 24 * HOUR


def day(ts):
    return datetime.fromtimestamp(ts // 1000).strftime('%Y-%m-%d')


class ForgetStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'eddie.db')
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        self.now = 100 * DAY + 12 * HOUR

    def insert(self, *rows):
        with self.conn:
            self.conn.executemany('INSERT INTO queries (ts, device_mac, domain, action) VALUES (?, ?, ?, ?)',
                                  [(ts, mac, domain, 'allow') for ts, mac, domain in rows])

    def domains(self):
        return [r[0] for r in self.conn.execute('SELECT domain FROM queries ORDER BY id')]

    def summaries(self):
        return self.conn.execute('SELECT day, device_mac, category, lookups FROM sensitive_summaries'
                                 ' ORDER BY day, device_mac, category').fetchall()

    def test_deletes_only_sensitive_rows_older_than_a_day(self):
        old = self.now - DAY - 1
        self.insert((old, 'aa', 'www.goodrx.com'), (old, 'aa', 'quran.com'), (old, 'aa', 'pbskids.org'),
                    (old, 'aa', 'netflix.com'), (self.now - DAY + 1, 'aa', 'cvs.com'), (self.now, 'aa', 'npr.org'))
        self.assertEqual(3, store.forget(self.conn, self.now))
        self.assertEqual(['netflix.com', 'cvs.com', 'npr.org'], self.domains())
        self.assertEqual(0, store.forget(self.conn, self.now))

    def test_summaries_keep_counts_by_day_device_and_group_only(self):
        a, b = self.now - 2 * DAY, self.now - 3 * DAY
        self.insert((a, 'aa', 'goodrx.com'), (a, 'aa', 'teladoc.com'), (a, 'bb', 'cvs.com'),
                    (a, 'aa', 'nytimes.com'), (a, 'aa', 'gop.com'), (b, 'aa', 'roblox.com'), (b, None, 'roblox.com'))
        store.forget(self.conn, self.now)
        self.assertEqual(sorted([(day(a), 'aa', 'beliefs', 2), (day(a), 'aa', 'health', 2), (day(a), 'bb', 'health', 1),
                                 (day(b), '', 'kids', 1), (day(b), 'aa', 'kids', 1)]), self.summaries())
        self.assertEqual(['day', 'device_mac', 'category', 'lookups'],
                         [r[1] for r in self.conn.execute('PRAGMA table_info(sensitive_summaries)')])

    def test_later_passes_add_to_the_same_day(self):
        ts = self.now - 2 * DAY
        self.insert((ts, 'aa', 'cvs.com'))
        store.forget(self.conn, self.now)
        self.insert((ts + 1, 'aa', 'walgreens.com'))  # e.g. a row the writer flushed late
        store.forget(self.conn, self.now + HOUR)
        self.assertEqual([(day(ts), 'aa', 'health', 2)], self.summaries())

    def test_verdicts_and_baseline_domains_for_sensitive_domains_go_too(self):
        old = self.now - DAY - 1
        with self.conn:
            self.conn.executemany('INSERT INTO ai_verdicts (domain, label, ts) VALUES (?, ?, ?)',
                                  [('betterhelp.com', 'benign', old), ('example.com', 'benign', old),
                                   ('cvs.com', 'benign', self.now)])
            self.conn.executemany('INSERT INTO baseline_domains (device_mac, domain, last_seen) VALUES (?, ?, ?)',
                                  [('aa', 'chabad.org', old), ('aa', 'example.com', old), ('aa', 'cvs.com', self.now)])
        self.assertEqual(2, store.forget(self.conn, self.now))
        self.assertEqual([('cvs.com',), ('example.com',)],
                         self.conn.execute('SELECT domain FROM ai_verdicts ORDER BY domain').fetchall())
        self.assertEqual([('cvs.com',), ('example.com',)],
                         self.conn.execute('SELECT domain FROM baseline_domains ORDER BY domain').fetchall())
        self.assertEqual([], self.summaries())  # verdicts and baselines aren't lookups

    def test_prune_runs_forget_and_ages_out_summaries(self):
        self.insert((self.now - 2 * DAY, 'aa', 'cvs.com'), (self.now - 2 * DAY, 'aa', 'netflix.com'))
        self.assertEqual(1, store.prune(self.conn, 7, now_ms=self.now))
        self.assertEqual(['netflix.com'], self.domains())
        self.assertEqual(1, len(self.summaries()))
        later = self.now + 7 * DAY
        self.assertEqual(2, store.prune(self.conn, 7, now_ms=later))  # the netflix row and the summary
        self.assertEqual([], self.summaries())

    def test_deleted_rows_are_overwritten(self):
        self.insert((self.now - 2 * DAY, 'aa', 'secret-marker.goodrx.com'))
        self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        store.prune(self.conn, 7, now_ms=self.now)
        self.conn.close()
        self.assertNotIn(b'secret-marker', Path(self.db).read_bytes())
        self.conn = store.connect(self.db)

    def test_hourly_writer_pass_forgets(self):
        self.conn.close()
        qlog = store.QueryLog(self.db, prune_every=0.05)
        qlog.log('cvs.com', 'aa', 'A', 'allow', 'default', ts=int(time.time() * 1000) - 2 * DAY)
        qlog.log('cvs.com', 'aa', 'A', 'allow', 'default')
        time.sleep(0.5)
        qlog.close()
        self.conn = store.connect(self.db)
        self.assertEqual(['cvs.com'], self.domains())
        self.assertEqual(1, self.summaries()[0][3])


def event(domain, category, ts):
    return {'ts': ts, 'device': 'TV', 'domain': domain, 'action': 'allow', 'company': 'x', 'category': category}


class ForgetFeed(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time() * 1000)
        self.feed = deque([event('cvs.com', 'health', self.now - DAY - 60_000),
                           event('netflix.com', 'streaming', self.now - DAY - 60_000),
                           event('quran.com', 'religion', self.now - DAY + 60_000)], maxlen=20000)
        for patcher in (patch.object(api, 'recent', self.feed), patch.dict(api.forget_state, {'swept': 0}),
                        patch.dict('os.environ', {'EDDIE_DB': 'off'})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.app, client=('127.0.0.1', 50000))

    def domains(self):
        return [e['domain'] for e in self.feed]

    def test_reads_drop_day_old_sensitive_events_only(self):
        self.assertEqual(['netflix.com', 'quran.com'], [e['domain'] for e in self.client.get('/events').json()])

    def test_sweep_runs_at_most_once_a_minute(self):
        api.feed()
        self.feed.appendleft(event('cvs.com', 'health', self.now - 2 * DAY))
        api.feed()
        self.assertIn('cvs.com', self.domains())
        with patch('eddie.api.time.time', return_value=(self.now + api.FORGET_EVERY_MS + 1000) / 1000):
            api.feed()
        self.assertNotIn('cvs.com', self.domains())

    def test_ingest_never_keeps_a_day_old_sensitive_event(self):
        body = [{'ts': self.now - 2 * DAY, 'device_mac': 'aa', 'device': 'TV', 'domain': 'goodrx.com',
                 'action': 'allow', 'source': 'blocklist'},
                {'ts': self.now, 'device_mac': 'aa', 'device': 'TV', 'domain': 'goodrx.com',
                 'action': 'allow', 'source': 'blocklist'}]
        self.assertEqual(200, self.client.post('/ingest', json=body).status_code)
        self.assertEqual([self.now], [e['ts'] for e in self.feed if e['domain'] == 'goodrx.com'])


if __name__ == '__main__':
    unittest.main()
