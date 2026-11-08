import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eddie import store, traffic

FIXTURE = Path(__file__).parent / "fixtures" / "traffic.pcap"
EDDIE = "02:ed:d1:e0:00:01"
PHONE = "02:00:00:00:00:0a"
TV = "02:00:00:00:00:0b"
T0 = 1_800_000_000_250

ROWS = {  # (device, ip, port, proto) -> (bytes_out, bytes_in, packets_out, packets_in)
    (PHONE, "142.250.80.46", 443, "tcp"): (2080, 240, 2, 1),
    (TV, "8.8.8.8", 3478, "udp"): (78, 0, 1, 0),
    (TV, "2606:4700:4700::1111", 443, "tcp"): (360, 0, 1, 0),
    (PHONE, "1.1.1.1", 0, "icmp"): (43, 0, 1, 0),
}


class ParseTests(unittest.TestCase):
    def frames(self):
        return [frame for _, frame in traffic.read_pcap(str(FIXTURE))]

    def test_fixture_headers_parse(self):
        pkt = traffic.parse_frame(self.frames()[0])
        self.assertEqual(traffic.Packet(PHONE, EDDIE, "192.168.4.10", "142.250.80.46", "tcp", 51000, 443, 1040), pkt)

    def test_vlan_and_ipv6(self):
        frames = self.frames()
        self.assertEqual(("8.8.8.8", 3478, "udp"), (lambda p: (p.dst_ip, p.dst_port, p.proto))(traffic.parse_frame(frames[3])))
        self.assertEqual("2606:4700:4700::1111", traffic.parse_frame(frames[4]).dst_ip)

    def test_non_ip_and_truncated_frames_are_skipped(self):
        frames = self.frames()
        self.assertIsNone(traffic.parse_frame(frames[8]))  # ARP
        self.assertIsNone(traffic.parse_frame(frames[9]))  # cut short
        self.assertIsNone(traffic.parse_frame(b""))

    def test_later_ipv4_fragment_has_no_ports(self):
        frame = bytearray(self.frames()[0])
        struct.pack_into("!H", frame, 20, 185)  # fragment offset, not the first fragment
        self.assertEqual((0, 0), (lambda p: (p.src_port, p.dst_port))(traffic.parse_frame(bytes(frame))))

    def test_pcap_timestamps_in_ms(self):
        self.assertEqual([T0 + i * 1000 for i in range(10)], [ts for ts, _ in traffic.read_pcap(str(FIXTURE))])

    def test_non_pcap_is_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".pcap") as f:
            f.write(b"\x0a\x0d\x0d\x0a" + bytes(40))  # pcapng
            f.flush()
            with self.assertRaises(ValueError):
                list(traffic.read_pcap(f.name))


class CounterTests(unittest.TestCase):
    def count(self, router_mac):
        counter = traffic.TrafficCounter(router_mac)
        for ts, frame in traffic.read_pcap(str(FIXTURE)):
            pkt = traffic.parse_frame(frame)
            if pkt:
                counter.add(pkt, ts)
        return {row[2:6]: row[6:] for row in counter.drain()}, counter

    def test_counts_per_device_and_remote_with_router_mac(self):
        rows, counter = self.count(EDDIE)
        self.assertEqual(ROWS, rows)
        self.assertEqual({}, counter.flows)  # drain empties

    def test_without_router_mac_global_ipv6_devices_are_missed(self):
        rows, _ = self.count(None)
        expected = dict(ROWS)
        del expected[(TV, "2606:4700:4700::1111", 443, "tcp")]
        self.assertEqual(expected, rows)

    def test_lan_and_dns_to_eddie_are_ignored(self):
        rows, _ = self.count(EDDIE)
        self.assertFalse({k for k in rows if k[1].startswith("192.168.")})

    def test_full_table_drops_new_flows_but_counts_known_ones(self):
        counter = traffic.TrafficCounter(EDDIE, max_flows=1)
        frames = [traffic.parse_frame(f) for _, f in traffic.read_pcap(str(FIXTURE))]
        self.assertTrue(counter.add(frames[0], 1))
        self.assertFalse(counter.add(frames[3], 2))
        self.assertTrue(counter.add(frames[1], 3))
        self.assertEqual(1, counter.dropped)
        self.assertEqual([(1, 3, PHONE, "142.250.80.46", 443, "tcp", 2080, 0, 2, 0)], counter.drain())


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "eddie.db")

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(f"SELECT {', '.join(traffic.COLUMNS)} FROM traffic").fetchall()

    def test_replay_writes_only_header_counts(self):
        conn = store.connect(self.db)
        self.assertEqual(6, traffic.run(traffic.read_pcap(str(FIXTURE)), conn, EDDIE))
        conn.close()
        rows = self.rows()
        self.assertEqual(ROWS, {r[2:6]: r[6:] for r in rows})
        phone = next(r for r in rows if r[3] == "142.250.80.46")
        self.assertEqual((T0, T0 + 2000), phone[:2])
        self.assertNotIn(b"SECRET-PAYLOAD", Path(self.db).read_bytes())

    def test_traffic_table_has_only_the_allowed_columns(self):
        store.connect(self.db).close()
        with sqlite3.connect(self.db) as conn:
            columns = [r[1] for r in conn.execute("PRAGMA table_info(traffic)")]
        self.assertEqual(["id", *traffic.COLUMNS], columns)

    def test_flushes_on_interval(self):
        ticks = iter(range(100))
        conn = store.connect(self.db)
        frames = list(traffic.read_pcap(str(FIXTURE)))
        flushed = []
        real_write = traffic.write
        with patch.object(traffic, "write", side_effect=lambda c, r: (flushed.append(len(r)), real_write(c, r))):
            traffic.run(iter(frames), conn, EDDIE, flush_every=3, clock=lambda: next(ticks))
        conn.close()
        self.assertGreater(len(flushed), 2)
        self.assertEqual(sum(v[2] + v[3] for v in ROWS.values()), sum(r[8] + r[9] for r in self.rows()))

    def test_old_traffic_is_pruned_with_the_query_log(self):
        conn = store.connect(self.db)
        traffic.write(conn, [(1, 2, PHONE, "1.1.1.1", 0, "icmp", 1, 0, 1, 0),
                             (T0, T0, PHONE, "1.1.1.1", 0, "icmp", 1, 0, 1, 0)])
        self.assertEqual(1, store.prune(conn, 7, now_ms=T0))
        conn.close()
        self.assertEqual(1, len(self.rows()))


class MainTests(unittest.TestCase):
    def test_off_by_default(self):
        with patch.dict("os.environ", {"EDDIE_DB": "unused.db"}, clear=True), \
                patch.object(traffic.store, "connect") as connect:
            self.assertEqual(2, traffic.main([]))
        connect.assert_not_called()

    def test_refuses_without_a_database(self):
        with patch.dict("os.environ", {"EDDIE_DB": "off"}, clear=True):
            self.assertEqual(2, traffic.main(["--pcap", str(FIXTURE)]))

    def test_pcap_replay_from_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "eddie.db")
            with patch.dict("os.environ", {"EDDIE_DB": db}, clear=True):
                self.assertEqual(0, traffic.main(["--pcap", str(FIXTURE), "--router-mac", EDDIE.upper()]))
            with sqlite3.connect(db) as conn:
                self.assertEqual(4, conn.execute("SELECT count(*) FROM traffic").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
