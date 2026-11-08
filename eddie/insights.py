"""Spoken traffic summary built only from real counts (served at /summary)."""
import time
from collections import defaultdict
from . import labels


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary_text(events: list[dict], now_ms: int | None = None) -> str:
    now_ms = now_ms or int(time.time() * 1000)
    hour = [e for e in events if 0 <= now_ms - e["ts"] < 60 * 60 * 1000]
    if not hour:
        return "Quiet so far. Nobody's tried anything yet."
    blocked = [e for e in hour if e["action"] == "block"]
    devices = {e["device"] for e in hour}
    text = (f"In the last hour I saw {plural(len(hour), 'request')} from "
            f"{plural(len(devices), 'device')} and blocked {len(blocked)}.")
    if blocked:
        counts = defaultdict(int)
        for e in blocked:
            counts[(e["company"], e["category"])] += 1
        (company, category), n = max(counts.items(), key=lambda kv: kv[1])
        text += f" The biggest offender was {company} {category}, {plural(n, 'time')}."
    return text


def company_counts(events: list[dict], now_ms: int | None = None, limit: int = 10) -> list[dict]:
    """Counts are requested lookups, not proof of uploaded data or unique visits."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    companies = {}
    for event in events:
        if not 0 <= now_ms - event['ts'] < 3_600_000:
            continue
        company, category = labels.lookup(event['domain'])
        row = companies.setdefault(company, {'company': company, 'requests': 0, 'blocked': 0, 'tracking_requests': 0})
        row['requests'] += 1
        row['blocked'] += event['action'] == 'block'
        row['tracking_requests'] += category in labels.TRACKING
    return sorted(companies.values(), key=lambda row: (-row['requests'], row['company']))[:limit]


WINDOWS_MS = {'15m': 900_000, '1h': 3_600_000, '24h': 86_400_000}


def device_counts(events: list[dict], window_ms: int, now_ms: int | None = None) -> list[dict]:
    """Per-device lookup counts in the window, with the companies blocked most."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    devices = {}
    for event in events:
        if not 0 <= now_ms - event['ts'] < window_ms:
            continue
        row = devices.setdefault(event['device'], {'device': event['device'], 'requests': 0, 'blocked': 0,
                                                   'flagged': 0, 'blocked_companies': defaultdict(int)})
        row['requests'] += 1
        row['blocked'] += event['action'] == 'block'
        row['flagged'] += event['action'] == 'flag'
        if event['action'] == 'block':
            row['blocked_companies'][labels.lookup(event['domain'])[0]] += 1
    for row in devices.values():
        top = sorted(row['blocked_companies'].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        row['blocked_companies'] = [{'company': company, 'blocked': n} for company, n in top]
    return sorted(devices.values(), key=lambda row: (-row['requests'], row['device']))


def broker_counts(events: list[dict], window_ms: int, now_ms: int | None = None, limit: int = 10) -> dict:
    """Lookups of known data broker and ad exchange domains in the window, per device and company."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    totals = {'requests': 0, 'blocked': 0}
    devices, companies = {}, {}
    for event in events:
        if not 0 <= now_ms - event['ts'] < window_ms:
            continue
        entry = labels.broker(event['domain'])
        if entry is None:
            continue
        blocked = event['action'] == 'block'
        device = devices.setdefault(event['device'], {'device': event['device'], 'requests': 0, 'blocked': 0})
        company = companies.setdefault(entry['company'], {**entry, 'requests': 0, 'blocked': 0})
        for row in (totals, device, company):
            row['requests'] += 1
            row['blocked'] += blocked
    order = lambda key: (lambda row: (-row['requests'], row[key]))
    return {**totals, 'devices': sorted(devices.values(), key=order('device')),
            'companies': sorted(companies.values(), key=order('company'))[:limit]}


def tracker_map(events: list[dict], window_ms: int, now_ms: int | None = None, limit: int = 12) -> dict:
    """Which tracking companies each device looked up in the window (#107), most devices first.

    A lookup shows the device reached the company, not what it sent. device_count counts every
    device with any lookup in the window; id_sync lists each company's ad-ID sync domains.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    seen, companies = set(), {}
    for event in events:
        if not 0 <= now_ms - event['ts'] < window_ms:
            continue
        seen.add(event['device'])
        company, category = labels.lookup(event['domain'])
        if category not in labels.TRACKING:
            continue
        blocked = event['action'] == 'block'
        row = companies.setdefault(company, {'company': company, 'requests': 0, 'blocked': 0,
                                             'devices': defaultdict(int), 'id_sync': {}})
        row['requests'] += 1
        row['blocked'] += blocked
        row['devices'][event['device']] += 1
        if domain := labels.id_sync(event['domain']):
            sync = row['id_sync'].setdefault(domain, {'domain': domain, 'requests': 0, 'blocked': 0, 'devices': set()})
            sync['requests'] += 1
            sync['blocked'] += blocked
            sync['devices'].add(event['device'])
    rows = []
    for row in companies.values():
        devices = sorted(row['devices'].items(), key=lambda kv: (-kv[1], kv[0]))
        rows.append({**row, 'device_count': len(devices),
                     'devices': [{'device': device, 'requests': n} for device, n in devices],
                     'id_sync': [{**sync, 'devices': sorted(sync['devices'])}
                                 for sync in sorted(row['id_sync'].values(), key=lambda s: s['domain'])]})
    rows.sort(key=lambda row: (-row['device_count'], -row['requests'], row['company']))
    top = rows[0] if rows else None
    text = (f"{top['company']} sees {top['device_count']} of your {plural(len(seen), 'device')}." if top
            else "No tracking lookups in this window.")
    return {'device_count': len(seen), 'devices': sorted(seen), 'companies': rows[:limit], 'text': text}
