"""Tests for rebuilding the nftables table after a reboot (eddie/nft.py)."""
import subprocess
import unittest
from unittest import mock

from eddie import nft


def ok(*_args, **_kwargs):
    return subprocess.CompletedProcess([], 0, "", "")


class TableScriptTests(unittest.TestCase):
    def test_creates_set_chains_and_rules(self):
        script = nft.table_script("wlan0")
        self.assertIn("add table inet eddie", script)
        self.assertIn("add set inet eddie homeblock { type ipv4_addr; }", script)
        self.assertIn("type filter hook forward priority 0;", script)
        self.assertIn("type nat hook postrouting priority srcnat;", script)
        self.assertIn("add rule inet eddie forward_filter ip saddr @homeblock drop", script)
        self.assertIn("add rule inet eddie forward_filter ip daddr @homeblock drop", script)
        self.assertIn('iifname "wlan0" oifname "wlan0" masquerade', script)

    def test_flushes_chains_before_adding_rules_but_keeps_the_set(self):
        lines = nft.table_script("wlan0").splitlines()
        first_rule = next(i for i, line in enumerate(lines) if line.startswith("add rule"))
        self.assertLess(lines.index("flush chain inet eddie forward_filter"), first_rule)
        self.assertLess(lines.index("flush chain inet eddie postrouting"), first_rule)
        self.assertFalse(any(line.startswith("flush set") or line.startswith("delete") for line in lines))

    def test_empty_interface_leaves_out_nat(self):
        self.assertNotIn("masquerade", nft.table_script(""))

    def test_interface_comes_from_the_environment(self):
        with mock.patch.dict("os.environ", {"EDDIE_NAT_IFACE": "eth0"}):
            self.assertIn('iifname "eth0" oifname "eth0" masquerade', nft.table_script())
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIn('iifname "wlan0"', nft.table_script())


class EnsureTableTests(unittest.TestCase):
    def test_sends_the_script_on_stdin_through_sudo(self):
        with mock.patch.object(nft, "is_available", return_value=True), \
                mock.patch("os.geteuid", return_value=1000), \
                mock.patch("subprocess.run", side_effect=ok) as run:
            self.assertTrue(nft.ensure_table())
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["sudo", "-n", "nft", "-f", "-"])
        self.assertIn("add table inet eddie", kwargs["input"])

    def test_no_nft_is_a_no_op(self):
        with mock.patch.object(nft, "is_available", return_value=False), \
                mock.patch("subprocess.run") as run:
            self.assertFalse(nft.ensure_table())
        run.assert_not_called()

    def test_failure_is_reported_not_raised(self):
        failed = subprocess.CompletedProcess([], 1, "", "Operation not permitted")
        with mock.patch.object(nft, "is_available", return_value=True), \
                mock.patch("subprocess.run", return_value=failed):
            self.assertFalse(nft.ensure_table())

    def test_home_mode_on_rebuilds_the_table_before_adding_cameras(self):
        calls = []
        with mock.patch.object(nft, "is_available", return_value=True), \
                mock.patch.object(nft, "get_camera_ips", return_value={"172.20.10.5"}), \
                mock.patch.object(nft, "_run_nft", side_effect=lambda args, script=None: calls.append(args) or True):
            self.assertTrue(nft.apply_home_mode(True))
        self.assertEqual(calls[0], ["-f", "-"])
        self.assertEqual(calls[1], ["add", "element", "inet", "eddie", "homeblock", "{ 172.20.10.5 }"])

    def test_home_mode_off_only_flushes_the_set(self):
        with mock.patch.object(nft, "is_available", return_value=True), \
                mock.patch.object(nft, "_run_nft", return_value=True) as run:
            self.assertTrue(nft.apply_home_mode(False))
        run.assert_called_once_with(["flush", "set", "inet", "eddie", "homeblock"])


if __name__ == "__main__":
    unittest.main()
