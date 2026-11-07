# Event contract (agree on this with Will before anything else)

The DNS server (Will) and the fake traffic generator (`scripts/fake_traffic.py`)
send **the exact same JSON** to the API. Swapping fake for real traffic means
pointing a different sender at the same endpoint. Nothing else changes.

## Sending: `POST /ingest`

Body is one event or a list of events.

```json
{
  "type": "query",
  "ts": 1762531200123,
  "device_mac": "f0:4f:7c:11:22:33",
  "device": "Fire Stick",
  "domain": "device-metrics-us.amazon.com",
  "qtype": "A",
  "action": "block",
  "source": "blocklist",
  "reason": "On the Amazon Fire TV tracker list"
}
```

| Field        | Type   | Notes |
|--------------|--------|-------|
| `type`       | string | always `"query"` |
| `ts`         | int    | milliseconds since epoch, when the query arrived |
| `device_mac` | string | MAC if known, otherwise `"ip:172.20.10.5"` |
| `device`     | string | friendly label (`"Fire Stick"`); fall back to the IP |
| `domain`     | string | lowercase, no trailing dot |
| `qtype`      | string | `A`, `AAAA`, `HTTPS`, ... |
| `action`     | string | `allow` \| `block` \| `flag` |
| `source`     | string | `rule` \| `allowlist` \| `blocklist` \| `heuristic` \| `ai` \| `home_mode` \| `default` |
| `reason`     | string | one short human line, shown on screen |

Will's side, from the DNS server, is fire-and-forget so DNS never waits:

```python
# in a background thread or asyncio task, never on the reply path
httpx.post("http://127.0.0.1:8000/ingest", json=event, timeout=1)
```

## Receiving: `WS /ws/events`

The API adds `id`, `company` and `category` (from `eddie/labels.py`) and pushes:

- `snapshot`: sent once on connect: `{events, totals, home_mode}`
- `query`: the event above plus `id`, `company`, `category`
- `totals`: `{seen, blocked}`
- `mode`: `{home_mode: bool}`

## Home mode: `GET/POST /mode`

`POST /mode {"home_mode": true}`. The decision engine reads `GET /mode` (or
shares the state in-process later) and, while on, blocks a device's
non-essential traffic with `source: "home_mode"`.

## Local voice bridge (port 5173)

The dashboard bridge SSE endpoint `/events` emits `voice_status` with `status` (`off`, `starting`, `ready`, `error`), optional `engine` (`vosk` prototype or `openwakeword`) and `message`. `GET /api/voice/status` returns the latest status. Start/stop POST endpoints operate the local microphone. These are separate from the traffic API on port 8000.

Wake lifecycle: `wake`, `voice_state: listening`, `voice_state: thinking`, optional `transcript`, `agent_reply`, `speaking`, `speech_end`, then `voice_state: listening` again for a follow-up without the wake phrase. If no speech starts within 4 seconds of listening, the worker emits `voice_state: dismissed` and the dashboard closes Eddie and returns to the live feed; the next request needs the wake phrase. No transcript is persisted. Capture or inference failure yields `voice_status: error`.


Voice recovery: after an initial wake with no command, or an empty/failed transcription, Eddie says “Didn’t catch that. Try again.” and listens again. Four seconds of silence during a follow-up dismisses Eddie back to the newest feed activity. Speech inference errors recover without disabling wake detection; microphone errors and bridge disconnects close the overlay. Listening/waiting uses a yellow ambient glow, speaking uses blue. Automated voice checks pass; live microphone/noisy-room accuracy still needs rehearsal.
