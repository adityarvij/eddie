"""nftables router mode enforcement (#22, #48): kernel-level packet filtering for camera streams.

On Linux with nftables, home mode adds non-security camera IPs to the `homeblock` set
to drop active packets mid-stream. Turning home mode off flushes the set.
On non-Linux platforms or systems without nftables, functions cleanly no-op.
"""
import logging
import os
import shutil
import sqlite3
import subprocess

from . import decision

log = logging.getLogger("eddie.nft")

TABLE = "inet eddie"
SET = "homeblock"


def is_available() -> bool:
    """Return True if nft is available on this system."""
    return bool(shutil.which("nft"))


def _run_nft(args: list[str]) -> bool:
    """Run an nft command via sudo or direct if root. Returns True on success."""
    if not is_available():
        return False
    is_root = getattr(os, "geteuid", lambda: -1)() == 0
    cmd = ["nft"] if is_root else ["sudo", "-n", "nft"]
    cmd.extend(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        if proc.returncode != 0:
            log.warning("nft failed (%s): %s", " ".join(cmd), proc.stderr.strip())
            return False
        return True
    except Exception as exc:
        log.warning("nft execution error (%s): %s", " ".join(cmd), exc)
        return False


def get_camera_ips(conn: sqlite3.Connection | None = None) -> set[str]:
    """Find IPs of active or known non-security camera devices."""
    ips: set[str] = set()

    # Check configured camera IPs from environment (e.g. demo camera)
    env_ips = os.environ.get("EDDIE_CAMERA_IPS", "")
    for item in env_ips.split(","):
        if item.strip():
            ips.add(item.strip())

    if not conn:
        return ips

    try:
        domains = list(decision.CAMERA_DOMAINS)
        placeholders = ",".join("?" for _ in domains)

        # 1. Devices that looked up a camera domain, excluding security-exempt devices
        query = f"""
            SELECT DISTINCT d.ip
            FROM devices d
            JOIN queries q ON (d.mac = q.device_mac OR q.device_mac = d.ip OR q.device_mac = 'ip:' || d.ip)
            WHERE d.security = 0
              AND d.ip IS NOT NULL
              AND d.ip != ''
              AND q.domain IN ({placeholders})
        """
        for row in conn.execute(query, domains).fetchall():
            if row[0]:
                ips.add(row[0])

        # 2. Queries logged directly under client IP before ARP resolved
        ip_query = f"""
            SELECT DISTINCT replace(q.device_mac, 'ip:', '')
            FROM queries q
            WHERE q.domain IN ({placeholders})
              AND (q.device_mac LIKE 'ip:%' OR q.device_mac LIKE '%.%.%.%')
        """
        for row in conn.execute(ip_query, domains).fetchall():
            cand = row[0]
            if cand and not cand.startswith("127."):
                # verify it's not marked as security
                sec_row = conn.execute("SELECT security FROM devices WHERE ip = ? OR mac = ?", (cand, cand)).fetchone()
                if not sec_row or sec_row[0] == 0:
                    ips.add(cand)

        # 3. Devices explicitly labeled as a camera/cam/larix (not marked security)
        cam_query = """
            SELECT ip FROM devices
            WHERE security = 0
              AND ip IS NOT NULL
              AND (
                lower(label) LIKE '%camera%' OR lower(label) LIKE '%cam%' OR lower(label) LIKE '%larix%' OR
                lower(hostname) LIKE '%camera%' OR lower(hostname) LIKE '%cam%' OR lower(hostname) LIKE '%larix%'
              )
        """
        for row in conn.execute(cam_query).fetchall():
            if row[0]:
                ips.add(row[0])
    except Exception as exc:
        log.warning("failed to query camera ips: %s", exc)

    return ips


def apply_home_mode(on: bool, conn: sqlite3.Connection | None = None) -> bool:
    """Enforce home mode via nftables packet filter.

    When on=True: adds camera IPs to @homeblock so packets are dropped mid-flight.
    When on=False: flushes @homeblock set so traffic resumes immediately.
    """
    if not is_available():
        return False

    if not on:
        return _run_nft(["flush", "set", "inet", "eddie", "homeblock"])

    ips = get_camera_ips(conn)
    if not ips:
        log.info("home mode nftables: no non-security camera IPs found to block")
        return True

    success = True
    for ip in ips:
        if not _run_nft(["add", "element", "inet", "eddie", "homeblock", f"{{ {ip} }}"]):
            success = False
        else:
            log.info("home mode nftables: blocked camera IP %s", ip)
    return success
