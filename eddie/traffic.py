"""Non-DNS traffic monitor (#136): per-device byte and packet counts.

Eddie's DNS server only sees lookups, so it misses upload volume, hard-coded
IPs and streams that outlive their lookup. This optional process reads packet
headers on Eddie's LAN interface and counts, per device, remote IP, remote
port and protocol: bytes and packets each way, and first / last seen.

It is off by default and runs as its own process (`python -m eddie.traffic`),
never in the DNS server, so DNS never waits on it. It only sees other devices'
packets when Eddie is their router (#22): on the hotspot those packets go
straight to the iPhone and the monitor sees nothing but Eddie's own traffic.

Only headers are parsed (Ethernet, 802.1Q, IPv4, IPv6, TCP, UDP). Payload
bytes are never read, kept or logged. Device-to-device traffic on the LAN and
traffic between two internet hosts are ignored. Live capture knows Eddie's
own MAC on the interface, so a frame sent to it is a device's upload and a
frame from it a download, counted when the remote address is global (IPv4 or
IPv6). Without that MAC (`--pcap` without `--router-mac`), a flow is counted
when one side is local (private, link-local, CGNAT) and the other global, so
IPv6 devices with global addresses are missed.

Counts sit in memory (at most MAX_FLOWS flows; new flows past that are dropped
and counted) and are written to eddie.db's `traffic` table every FLUSH_EVERY
seconds, one row per flow per flush, pruned with the query log.

Capture uses a Linux AF_PACKET raw socket (stdlib, needs root or CAP_NET_RAW)
on EDDIE_TRAFFIC_IFACE; `--pcap` replays a capture file instead (tests, and
checking a capture by hand). No scapy dependency.
"""
import argparse
import ipaddress
import logging
import os
import signal
import socket
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Iterator

from . import store

FLUSH_EVERY = 60.0  # seconds between writes to eddie.db
MAX_FLOWS = 10_000  # flows held between flushes
SNAPLEN = 128  # bytes read per frame: enough for every header we parse
COLUMNS = ("first_ts", "last_ts", "device_mac", "ip", "port", "proto",
           "bytes_out", "bytes_in", "packets_out", "packets_in")
ETH_P_ALL = 0x0003
PROTOS = {6: "tcp", 17: "udp", 1: "icmp", 58: "icmp"}

log = logging.getLogger("eddie.traffic")


@dataclass(frozen=True)
class Packet:
    src_mac: str
    dst_mac: str
    src_ip: str
    dst_ip: str
    proto: str
    src_port: int
    dst_port: int
    length: int  # IP packet length from its header: headers plus payload, as sent


def _mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def parse_frame(frame: bytes) -> Packet | None:
    """Headers of one Ethernet frame, or None for anything that isn't IP or is cut short."""
    if len(frame) < 14:
        return None
    ethertype = struct.unpack_from("!H", frame, 12)[0]
    offset = 14
    while ethertype in (0x8100, 0x88A8):  # VLAN tags
        if len(frame) < offset + 4:
            return None
        ethertype = struct.unpack_from("!H", frame, offset + 2)[0]
        offset += 4
    if ethertype == 0x0800:
        if len(frame) < offset + 20 or frame[offset] >> 4 != 4:
            return None
        ihl = (frame[offset] & 0x0F) * 4
        length = struct.unpack_from("!H", frame, offset + 2)[0]
        frag = struct.unpack_from("!H", frame, offset + 6)[0] & 0x1FFF
        number = frame[offset + 9]
        src, dst = frame[offset + 12:offset + 16], frame[offset + 16:offset + 20]
        transport = offset + ihl if frag == 0 else None  # later fragments carry no ports
    elif ethertype == 0x86DD:
        if len(frame) < offset + 40 or frame[offset] >> 4 != 6:
            return None
        length = 40 + struct.unpack_from("!H", frame, offset + 4)[0]
        number = frame[offset + 6]  # extension headers are counted with port 0
        src, dst = frame[offset + 8:offset + 24], frame[offset + 24:offset + 40]
        transport = offset + 40
    else:
        return None
    sport = dport = 0
    if number in (6, 17) and transport is not None and len(frame) >= transport + 4:
        sport, dport = struct.unpack_from("!HH", frame, transport)
    return Packet(_mac(frame[6:12]), _mac(frame[0:6]),
                  str(ipaddress.ip_address(src)), str(ipaddress.ip_address(dst)),
                  PROTOS.get(number, str(number)), sport, dport, length)


def _local(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not addr.is_global and not addr.is_multicast and not addr.is_unspecified


def _global(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_global and not addr.is_multicast


class TrafficCounter:
    """Per-flow counts between flushes. add() is the only per-packet work."""

    def __init__(self, router_mac: str | None = None, max_flows: int = MAX_FLOWS):
        self.router_mac = router_mac
        self.max_flows = max_flows
        self.flows: dict[tuple, list] = {}
        self.dropped = 0

    def add(self, pkt: Packet, ts: int) -> bool:
        """Count one packet; False if it isn't device-to-internet traffic or the table is full."""
        if self.router_mac:
            out = pkt.dst_mac == self.router_mac
            if not out and pkt.src_mac != self.router_mac:
                return False
        elif _local(pkt.src_ip) and _global(pkt.dst_ip):
            out = True
        elif _global(pkt.src_ip) and _local(pkt.dst_ip):
            out = False
        else:
            return False
        if out:
            key, remote = (pkt.src_mac, pkt.dst_ip, pkt.dst_port, pkt.proto), pkt.dst_ip
        else:
            key, remote = (pkt.dst_mac, pkt.src_ip, pkt.src_port, pkt.proto), pkt.src_ip
        if not _global(remote):  # Eddie's own replies, LAN-to-LAN
            return False
        flow = self.flows.get(key)
        if flow is None:
            if len(self.flows) >= self.max_flows:
                self.dropped += 1
                if self.dropped == 1 or self.dropped % 1000 == 0:
                    log.warning("traffic table full, %d new flows dropped", self.dropped)
                return False
            flow = self.flows[key] = [ts, ts, 0, 0, 0, 0]
        flow[1] = ts
        flow[2 if out else 3] += pkt.length
        flow[4 if out else 5] += 1
        return True

    def drain(self) -> list[tuple]:
        """Rows in COLUMNS order, and an empty table."""
        rows = [(f[0], f[1], *key, *f[2:]) for key, f in self.flows.items()]
        self.flows = {}
        return rows


def write(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    sql = f"INSERT INTO traffic ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"
    with conn:
        conn.executemany(sql, rows)


def read_pcap(path: str) -> Iterator[tuple[int, bytes]]:
    """(ts ms, frame) from a classic pcap file with Ethernet frames."""
    with open(path, "rb") as f:
        header = f.read(24)
        if len(header) < 24:
            raise ValueError("not a pcap file")
        magic = header[:4]
        endian = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",
                  b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">"}.get(magic)
        if endian is None:
            raise ValueError("not a pcap file (pcapng isn't supported)")
        nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
        if struct.unpack(endian + "I", header[20:24])[0] != 1:
            raise ValueError("only Ethernet captures are supported")
        while len(record := f.read(16)) == 16:
            sec, frac, caplen, _ = struct.unpack(endian + "IIII", record)
            frame = f.read(caplen)
            if len(frame) < caplen:
                return
            yield sec * 1000 + (frac // 1_000_000 if nano else frac // 1000), frame


def iface_mac(iface: str) -> str | None:
    """Eddie's own MAC on a Linux interface, from sysfs."""
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip().lower() or None
    except OSError:
        return None


def sniff(iface: str) -> Iterator[tuple[int, bytes]]:
    """(ts ms, frame headers) from a live interface, both directions. Linux only."""
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(ETH_P_ALL))
    sock.bind((iface, 0))
    sock.settimeout(1.0)  # wake to flush on a quiet network
    while True:
        try:
            frame = sock.recv(SNAPLEN)
        except socket.timeout:
            yield int(time.time() * 1000), b""
            continue
        yield int(time.time() * 1000), frame


def run(frames: Iterable[tuple[int, bytes]], conn: sqlite3.Connection, router_mac: str | None = None,
        flush_every: float = FLUSH_EVERY, retention_days: float = store.RETENTION_DAYS,
        clock=time.monotonic) -> int:
    """Count frames and flush to the traffic table; returns packets counted."""
    counter = TrafficCounter(router_mac)
    counted = 0
    last_flush = last_prune = clock()
    try:
        for ts, frame in frames:
            pkt = parse_frame(frame) if frame else None
            if pkt is not None and counter.add(pkt, ts):
                counted += 1
            if clock() - last_flush >= flush_every:
                last_flush = clock()
                write(conn, counter.drain())
                if last_flush - last_prune >= store.PRUNE_EVERY:
                    last_prune = last_flush
                    store.prune(conn, retention_days)
    finally:
        write(conn, counter.drain())
    return counted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eddie.traffic", description=__doc__.split("\n")[0])
    parser.add_argument("--iface", default=os.environ.get("EDDIE_TRAFFIC_IFACE"),
                        help="LAN interface to watch (EDDIE_TRAFFIC_IFACE), e.g. wlan0 in router mode")
    parser.add_argument("--pcap", help="replay a pcap file instead of a live interface")
    parser.add_argument("--router-mac", help="Eddie's MAC in the capture (default: the interface's own)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    path = os.environ.get("EDDIE_DB", "eddie.db")
    if path == "off":
        log.error("EDDIE_DB=off: nowhere to record traffic, not starting")
        return 2
    if not args.pcap and not args.iface:
        log.error("traffic monitor is off: set EDDIE_TRAFFIC_IFACE or pass --iface or --pcap")
        return 2
    if not args.pcap and not hasattr(socket, "AF_PACKET"):
        log.error("live capture needs Linux (AF_PACKET); use --pcap on %s", sys.platform)
        return 2
    conn = store.connect(path)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # finally-blocks flush on the way out
    retention = float(os.environ.get("EDDIE_RETENTION_DAYS", store.RETENTION_DAYS))
    router_mac = args.router_mac.lower() if args.router_mac else None
    if not args.pcap:
        router_mac = router_mac or iface_mac(args.iface)
    try:
        counted = run(read_pcap(args.pcap) if args.pcap else sniff(args.iface), conn, router_mac,
                      retention_days=retention)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()
    log.info("counted %d packets", counted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
