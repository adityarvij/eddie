"""Writes traffic.pcap, the traffic monitor's fixture (tests/test_traffic.py).

Hand-built frames, not a household capture: invented locally administered MACs,
private LAN addresses and well-known public servers. Re-run to regenerate:
python tests/fixtures/make_traffic_pcap.py
"""
import ipaddress
import struct
from pathlib import Path

EDDIE = "02:ed:d1:e0:00:01"
PHONE = "02:00:00:00:00:0a"
TV = "02:00:00:00:00:0b"
PAYLOAD = b"SECRET-PAYLOAD "  # tests check these bytes never reach eddie.db


def mac(m):
    return bytes.fromhex(m.replace(":", ""))


def eth(dst, src, ethertype, body, vlan=None):
    tag = struct.pack("!HH", 0x8100, vlan) if vlan is not None else b""
    return mac(dst) + mac(src) + tag + struct.pack("!H", ethertype) + body


def ipv4(src, dst, proto, body):
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(body), 1, 0, 64, proto, 0,
                         ipaddress.IPv4Address(src).packed, ipaddress.IPv4Address(dst).packed)
    return header + body


def ipv6(src, dst, proto, body):
    return (struct.pack("!IHBB", 6 << 28, len(body), proto, 64)
            + ipaddress.IPv6Address(src).packed + ipaddress.IPv6Address(dst).packed + body)


def tcp(sport, dport, size):
    return struct.pack("!HHIIBBHHH", sport, dport, 1, 0, 0x50, 0x18, 65535, 0, 0) + (PAYLOAD * 100)[:size]


def udp(sport, dport, size):
    return struct.pack("!HHHH", sport, dport, 8 + size, 0) + (PAYLOAD * 100)[:size]


FRAMES = [
    # phone uploads to a Google server twice, gets one reply
    eth(EDDIE, PHONE, 0x0800, ipv4("192.168.4.10", "142.250.80.46", 6, tcp(51000, 443, 1000))),
    eth(EDDIE, PHONE, 0x0800, ipv4("192.168.4.10", "142.250.80.46", 6, tcp(51000, 443, 1000))),
    eth(PHONE, EDDIE, 0x0800, ipv4("142.250.80.46", "192.168.4.10", 6, tcp(443, 51000, 200))),
    # TV sends UDP to a STUN server, VLAN-tagged
    eth(EDDIE, TV, 0x0800, ipv4("192.168.4.11", "8.8.8.8", 17, udp(5000, 3478, 50)), vlan=10),
    # TV with a global IPv6 address talks to Cloudflare: only seen with Eddie's MAC known
    eth(EDDIE, TV, 0x86DD, ipv6("2a01:4f8::b", "2606:4700:4700::1111", 6, tcp(52000, 443, 300))),
    # phone pings 1.1.1.1
    eth(EDDIE, PHONE, 0x0800, ipv4("192.168.4.10", "1.1.1.1", 1, b"\x08\x00\x00\x00\x00\x01\x00\x01" + PAYLOAD)),
    # ignored: DNS to Eddie, phone to TV on the LAN, ARP, a frame cut short
    eth(EDDIE, PHONE, 0x0800, ipv4("192.168.4.10", "192.168.4.1", 17, udp(53000, 53, 30))),
    eth(TV, PHONE, 0x0800, ipv4("192.168.4.10", "192.168.4.11", 6, tcp(40000, 8009, 100))),
    eth("ff:ff:ff:ff:ff:ff", PHONE, 0x0806, bytes(28)),
    eth(EDDIE, PHONE, 0x0800, b"\x45\x00\x00"),
]


def write(path):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for i, frame in enumerate(FRAMES):
        out += struct.pack("<IIII", 1_800_000_000 + i, 250_000, len(frame), len(frame)) + frame
    Path(path).write_bytes(out)


if __name__ == "__main__":
    write(Path(__file__).with_name("traffic.pcap"))
