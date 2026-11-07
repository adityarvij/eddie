import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from eddie import store

DAY = 86_400_000


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'eddie.db')

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, sql='SELECT ts, device_mac, domain, qtype, action, source, reason FROM queries ORDER BY id'):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(sql).fetchall()

    def test_schema_created_on_first_open_and_reopen_is_safe(self):
        store.connect(self.db).close()
        store.connect(self.db).close()
        names = {r[0] for r in self.rows("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
        self.assertLessEqual({'devices', 'queries', 'rules', 'ai_verdicts', 'q_ts', 'q_dev'}, names)

    def test_queries_table_has_only_the_allowed_columns(self):
        store.connect(self.db).close()
        columns = [r[1] for r in self.rows('PRAGMA table_info(queries)')]
        self.assertEqual(['id', *store.QUERY_COLUMNS], columns)

    def test_rows_written_in_order_and_flushed_on_close(self):
        qlog = store.QueryLog(self.db)
        for i in range(1200):  # more than one batch
            qlog.log(f'd{i}.example', '10.0.0.5', 'A', 'allow', 'default', ts=i)
        qlog.log('t.example', '10.0.0.6', 'AAAA', 'block', 'blocklist', 'stub', ts=5000)
        qlog.close()
        rows = self.rows()
        self.assertEqual(1201, len(rows))
        self.assertEqual((0, '10.0.0.5', 'd0.example', 'A', 'allow', 'default', ''), rows[0])
        self.assertEqual((5000, '10.0.0.6', 't.example', 'AAAA', 'block', 'blocklist', 'stub'), rows[-1])

    def test_full_queue_drops_instead_of_blocking(self):
        gate = threading.Event()

        class StalledLog(store.QueryLog):  # writer stuck, e.g. disk stalled
            def _write_loop(self):
                gate.wait()
                super()._write_loop()

        qlog = StalledLog(self.db, max_pending=2)
        start = time.perf_counter()
        for i in range(5):
            qlog.log(f'd{i}.example', 'ip', 'A', 'allow', 'default', ts=i)
        self.assertLess(time.perf_counter() - start, 0.1)
        self.assertEqual(3, qlog.dropped)
        gate.set()
        qlog.close()
        self.assertEqual([(0,), (1,)], self.rows('SELECT ts FROM queries ORDER BY id'))

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_db_wal_and_shm_are_owner_only(self):
        qlog = store.QueryLog(self.db)
        qlog.log('a.example', 'ip', 'A', 'allow', 'default')
        qlog.close()
        conn = store.connect(self.db)  # WAL files exist while a connection is open
        conn.execute('SELECT 1 FROM queries').fetchall()
        for path in (self.db, self.db + '-wal', self.db + '-shm'):
            self.assertTrue(os.path.exists(path), path)
            self.assertEqual(0o600, self.mode(path), path)
        conn.close()

    def test_tightens_an_existing_world_readable_db(self):
        store.connect(self.db).close()
        os.chmod(self.db, 0o644)
        store.connect(self.db).close()
        self.assertEqual(0o600, self.mode(self.db))

    def test_secure_delete_is_on(self):
        conn = store.connect(self.db)
        self.assertEqual(1, conn.execute('PRAGMA secure_delete').fetchone()[0])
        conn.close()

    def test_prune_deletes_only_rows_past_retention(self):
        now = 100 * DAY
        conn = store.connect(self.db)
        with conn:
            conn.executemany('INSERT INTO queries (ts, domain) VALUES (?, ?)',
                             [(now - 8 * DAY, 'old'), (now - 7 * DAY + 1, 'edge'), (now, 'new')])
        self.assertEqual(1, store.prune(conn, 7, now_ms=now))
        self.assertEqual(0, store.prune(conn, 7, now_ms=now))
        conn.close()
        self.assertEqual([('edge',), ('new',)], self.rows('SELECT domain FROM queries ORDER BY id'))

    def test_old_rows_pruned_on_open(self):
        now = int(time.time() * 1000)
        conn = store.connect(self.db)
        with conn:
            conn.executemany('INSERT INTO queries (ts, domain) VALUES (?, ?)',
                             [(now - 30 * DAY, 'old'), (now, 'new')])
        conn.close()
        store.QueryLog(self.db).close()
        self.assertEqual([('new',)], self.rows('SELECT domain FROM queries'))

    def test_idle_writer_still_prunes_on_schedule(self):
        qlog = store.QueryLog(self.db, prune_every=0.05)
        qlog.log('old.example', 'ip', 'A', 'allow', 'default', ts=int(time.time() * 1000) - 30 * DAY)
        time.sleep(0.5)  # no further lookups arrive
        count = self.rows('SELECT COUNT(*) FROM queries')
        qlog.close()
        self.assertEqual([(0,)], count)

    def test_env_off_disables_logging_and_writes_nothing(self):
        with patch.dict('os.environ', {'EDDIE_DB': 'off'}):
            self.assertIsNone(store.query_log_from_env())
        self.assertEqual([], os.listdir(self.tmp.name))

    def test_env_sets_path_and_retention(self):
        with patch.dict('os.environ', {'EDDIE_DB': self.db, 'EDDIE_RETENTION_DAYS': '1.5'}):
            qlog = store.query_log_from_env()
        qlog.close()
        self.assertEqual((self.db, 1.5), (qlog.path, qlog.retention_days))

if __name__ == '__main__':
    unittest.main()
