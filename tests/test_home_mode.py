"""Tests for Home mode: camera upload domains, block rule (#43) and security device exemptions (#46)."""
import os
import sqlite3
import tempfile
import unittest

from eddie import decision, devices, store
from eddie.decision import Engine, Rule, Verdict

TV = "aa:00:00:00:00:01"
INDOOR_CAM = "aa:00:00:00:00:02"
DOORBELL = "aa:00:00:00:00:03"
OUTDOOR_CAM = "aa:00:00:00:00:04"


def decide(engine: Engine, domain: str, device: str = INDOOR_CAM):
    d = engine.evaluate(domain, device)
    return d.action, d.source, d.reason


class HomeModeBlockTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.engine.set_lists(allowlist=["netflix.com"], blocklist=["tracker.example"])

    def test_camera_domains_blocked_when_home_mode_on(self):
        self.engine.set_home_mode(True)
        for domain in [
            "ring.com",
            "upload.ring.com",
            "api.ring.com",
            "media.ring.com",
            "arlo.com",
            "live.arlo.com",
            "fakecamera.example",
            "sub.fakecamera.example",
            "camera.local",
            "a.rtmp.youtube.com",
            "b.rtmp.youtube.com",
            "upload.youtube.com",
        ]:
            action, src, reason = decide(self.engine, domain, INDOOR_CAM)
            self.assertEqual(action, "block", f"Expected {domain} to be blocked")
            self.assertEqual(src, "home_mode", f"Expected source 'home_mode' for {domain}")
            self.assertIn("camera upload blocked", reason)

    def test_camera_domains_allowed_when_home_mode_off(self):
        self.engine.set_home_mode(False)
        for domain in [
            "upload.ring.com",
            "fakecamera.example",
            "camera.local",
            "a.rtmp.youtube.com",
        ]:
            action, src, _ = decide(self.engine, domain, INDOOR_CAM)
            self.assertNotEqual(src, "home_mode", f"{domain} should not have source 'home_mode' when off")
            self.assertNotEqual(action, "block", f"{domain} should not be blocked when home mode is off")

    def test_non_camera_domains_unaffected_by_home_mode(self):
        for home_state in (False, True):
            self.engine.set_home_mode(home_state)
            action, src, _ = decide(self.engine, "netflix.com", TV)
            self.assertEqual(action, "allow")
            self.assertEqual(src, "allowlist")

            action, src, _ = decide(self.engine, "tracker.example", TV)
            self.assertEqual(action, "block")
            self.assertEqual(src, "blocklist")

            # Unknown domain
            action, src, _ = decide(self.engine, "news.example", TV)
            self.assertEqual(action, "flag")
            self.assertEqual(src, "default")

    def test_allowlist_beats_home_mode(self):
        # Even if camera domain, allowlist takes precedence (Decision step 2 vs Home mode step)
        self.engine.set_lists(allowlist=["essential.ring.com"], blocklist=[])
        self.engine.set_home_mode(True)
        action, src, entry = decide(self.engine, "essential.ring.com", INDOOR_CAM)
        self.assertEqual(action, "allow")
        self.assertEqual(src, "allowlist")
        self.assertEqual(entry, "essential.ring.com")

    def test_explicit_user_rule_beats_home_mode(self):
        # A specific user rule overriding a camera domain
        self.engine.set_rules([Rule(1, "upload.ring.com", "allow", INDOOR_CAM)])
        self.engine.set_home_mode(True)
        action, src, _ = decide(self.engine, "upload.ring.com", INDOOR_CAM)
        self.assertEqual(action, "allow")
        self.assertEqual(src, "rule")

    def test_doorbell_and_security_devices_exempt_from_home_mode(self):
        """Ticket #46: Doorbell traffic allowed with home mode on; only indoor cameras are affected."""
        self.engine.set_home_mode(True)
        self.engine.set_security_devices([DOORBELL, OUTDOOR_CAM])

        # Indoor camera is blocked
        action_indoor, src_indoor, _ = decide(self.engine, "upload.ring.com", INDOOR_CAM)
        self.assertEqual(action_indoor, "block")
        self.assertEqual(src_indoor, "home_mode")

        # Doorbell is allowed (exempt from home mode)
        action_doorbell, src_doorbell, _ = decide(self.engine, "upload.ring.com", DOORBELL)
        self.assertNotEqual(action_doorbell, "block")
        self.assertNotEqual(src_doorbell, "home_mode")

        # Outdoor camera is allowed
        action_outdoor, src_outdoor, _ = decide(self.engine, "upload.ring.com", OUTDOOR_CAM)
        self.assertNotEqual(action_outdoor, "block")
        self.assertNotEqual(src_outdoor, "home_mode")

    def test_load_from_store_syncs_security_devices(self):
        """Ticket #46: Security devices are auto-detected by naming or explicit security flag in DB."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test_eddie.db")
            conn = store.connect(db_path)
            try:
                # Insert devices into DB:
                # 1. Doorbell tagged by label
                conn.execute(
                    "INSERT INTO devices (mac, ip, hostname, vendor, label, first_seen, last_seen, security) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (DOORBELL, "192.168.1.50", "ring-bell", "Ring", "Front Doorbell", 1000, 1000, 0),
                )
                # 2. Outdoor cam tagged by hostname
                conn.execute(
                    "INSERT INTO devices (mac, ip, hostname, vendor, label, first_seen, last_seen, security) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (OUTDOOR_CAM, "192.168.1.51", "floodlight-cam-outdoor", "Ring", None, 1000, 1000, 0),
                )
                # 3. Explicit security flag = 1
                EXPLICIT_SEC = "aa:00:00:00:00:05"
                conn.execute(
                    "INSERT INTO devices (mac, ip, hostname, vendor, label, first_seen, last_seen, security) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (EXPLICIT_SEC, "192.168.1.52", "device-x", "Unknown", None, 1000, 1000, 1),
                )
                # 4. Indoor camera (not a security device)
                conn.execute(
                    "INSERT INTO devices (mac, ip, hostname, vendor, label, first_seen, last_seen, security) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (INDOOR_CAM, "192.168.1.53", "living-room-indoor", "Ring", "Living Room Cam", 1000, 1000, 0),
                )
                # Turn Home Mode on in DB
                conn.execute(
                    "INSERT INTO engine_state (key, value) VALUES ('home_mode', 1) "
                    "ON CONFLICT(key) DO UPDATE SET value = 1"
                )
                conn.commit()

                # Load into engine
                self.engine.load_from_store(conn)
                self.assertTrue(self.engine.home_mode)

                # Indoor cam blocked
                action, src, _ = decide(self.engine, "upload.ring.com", INDOOR_CAM)
                self.assertEqual(action, "block")
                self.assertEqual(src, "home_mode")

                # All 3 security devices allowed
                for sec_mac in [DOORBELL, OUTDOOR_CAM, EXPLICIT_SEC]:
                    action, src, _ = decide(self.engine, "upload.ring.com", sec_mac)
                    self.assertNotEqual(action, "block", f"{sec_mac} should not be blocked")
                    self.assertNotEqual(src, "home_mode", f"{sec_mac} should not have source home_mode")
            finally:
                conn.close()

    def test_load_from_store_syncs_home_mode_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test_eddie.db")
            conn = store.connect(db_path)
            try:
                # Initially off
                self.engine.load_from_store(conn)
                self.assertFalse(self.engine.home_mode)

                # Set on in engine_state
                conn.execute(
                    "INSERT INTO engine_state (key, value) VALUES ('home_mode', 1) "
                    "ON CONFLICT(key) DO UPDATE SET value = 1"
                )
                conn.commit()

                self.engine.load_from_store(conn)
                self.assertTrue(self.engine.home_mode)
                action, src, _ = decide(self.engine, "upload.ring.com", INDOOR_CAM)
                self.assertEqual(action, "block")
                self.assertEqual(src, "home_mode")

                # Set off in engine_state
                conn.execute("UPDATE engine_state SET value = 0 WHERE key = 'home_mode'")
                conn.commit()

                self.engine.load_from_store(conn)
                self.assertFalse(self.engine.home_mode)
                action, src, _ = decide(self.engine, "upload.ring.com", INDOOR_CAM)
                self.assertNotEqual(action, "block")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
