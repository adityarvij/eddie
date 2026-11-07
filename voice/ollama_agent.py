"""Local Ollama tool-calling agent with read-only traffic tools.

The model only phrases answers; every fact comes from the local Eddie API
through the tools below. Nothing here can change a rule, mode or device.

- EDDIE_OLLAMA_URL must be localhost or a private IP literal; proxies and
  redirects are ignored, so questions and traffic facts stay in the house.
- Every number in the reply must appear in a tool result, otherwise the reply
  is replaced by backend text or an honest "can't verify".
- A backend failure ends the turn with a fixed spoken failure; a model failure
  raises AgentUnavailable so the caller can use the deterministic router.
- Questions, tool results and replies are never logged or stored.

Typed test:  python voice/ollama_agent.py "What's the TV doing?"
"""
import ipaddress
import json
import os
import re
import time
import urllib.parse
import urllib.request

import voice_agent

DEFAULT_URL = 'http://127.0.0.1:11434'
DEFAULT_MODEL = 'llama3.2:3b'
MAX_ROUNDS = 4  # model calls per question, including the final answer
CALL_TIMEOUT = 20.0
DEADLINE = 30.0
MAX_REPLY_CHARS = 400
DOMAIN = re.compile(r'[a-z0-9_-]+(?:\.[a-z0-9_-]+)+')
TV_WORDS = ('tv', 'fire stick', 'firestick', 'roku', 'chromecast')
ALL_DEVICES = {'', 'all', 'any', 'every', 'everything', 'all devices', 'every device', 'any device', 'none'}
WINDOW_WORDS = {'15m': '15 minutes', '1h': 'hour', '24h': '24 hours'}

SYSTEM_PROMPT = (
    'You are Eddie, a local privacy guard. Answer in two short sentences, spoken style, a little dry. '
    'Use tools for facts; never invent numbers. Confirm before sending email or locking down a device. '
    'Your tools only read traffic; you cannot block, allow or change anything, so never say you did. '
    'Use the device names the tools return. If a tool reports an error, say plainly what you could not find. '
    'Only state facts that appear in tool results; when a result has a "said" sentence, base your answer on it.'
)
BACKEND_FAILURE = "I can't reach Eddie's traffic backend, so I can't answer that right now."
UNVERIFIED = "I couldn't check that answer against Eddie's records, so I won't guess."


class AgentUnavailable(Exception):
    """The local model could not produce a usable answer."""


class ToolError(ValueError):
    """Bad tool arguments; reported back to the model, not spoken directly."""


class BackendError(Exception):
    """The Eddie API failed; the turn ends with BACKEND_FAILURE."""


def check_local(url):
    """Raise ValueError unless url is http(s) to localhost or a private IP literal."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError(f'{url} is not an http(s) URL')
    if parts.hostname == 'localhost':
        return
    try:
        addr = ipaddress.ip_address(parts.hostname)
    except ValueError:
        raise ValueError(f'{parts.hostname} is a hostname; use localhost or a private IP')
    if not (addr.is_loopback or addr.is_private) or addr.is_unspecified:
        raise ValueError(f'{addr} is not a loopback or private address')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def function(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}}}


TOOLS = [
    function('get_summary', 'Request and block counts per household device, with the companies blocked most. '
             'Use window 1h unless the user names a time span. Omit device to see every device.',
             {'window': {'type': 'string', 'enum': ['15m', '1h', '24h']},
              'device': {'type': 'string', 'description': 'Device name, for example "TV" or "Fire Stick".'}},
             ['window']),
    function('get_recent_blocks', 'The most recently blocked domains, newest first, with the reason for each.',
             {'device': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}}),
    function('explain_domain', "Eddie's latest decision and any local AI verdict for one domain.",
             {'domain': {'type': 'string'}}, ['domain']),
    voice_agent.WHAT_THEY_KNOW_TOOL,
]


def read(path):
    try:
        return voice_agent.request(path)
    except Exception as error:
        raise BackendError(str(error)) from error


def find_device(name, devices):
    """The device rows matching a spoken name; TV words match TV-like labels."""
    wanted = ' '.join(re.sub(r'[^a-z0-9 ]', '', name.lower()).split())
    wanted = re.sub(r'^(?:the|my|our) ', '', wanted)
    exact = [d for d in devices if d['device'].lower() == wanted]
    if exact:
        return exact
    if wanted in ('tv', 'television', 'telly'):
        return [d for d in devices if any(word in d['device'].lower() for word in TV_WORDS)]
    return [d for d in devices if wanted and (wanted in d['device'].lower() or d['device'].lower() in wanted)]


def device_filter(device):
    """None for "every device", else the spoken device name."""
    if device is None:
        return None
    if not isinstance(device, str):
        raise ToolError('device must be a string')
    return None if device.strip().lower() in ALL_DEVICES else device


def describe(row, window):
    """One factual sentence per device, built from the counts so the model can repeat it."""
    n = row['requests']
    said = f"{row['device']} made {n} request{'' if n == 1 else 's'} in the last {WINDOW_WORDS[window]}"
    said += f" and Eddie blocked {row['blocked']}"
    if row.get('blocked_companies'):
        top = row['blocked_companies'][0]
        said += f", {top['blocked']} of them to {top['company']}"
    return {**row, 'said': said + '.'}


def unknown_device(name, devices):
    raise ToolError({'error': f'No device called {name!r} was seen in this window.',
                     'known_devices': [d['device'] for d in devices]})


def get_summary(window='1h', device=None):
    if window not in ('15m', '1h', '24h'):
        raise ToolError('window must be 15m, 1h or 24h')
    device = device_filter(device)
    data = read(f'/summary?window={window}')
    devices = data.get('devices', [])
    if device is None:
        return {'window': window, 'devices': [describe(d, window) for d in devices],
                'spoken_summary_last_hour': data.get('text')}
    matches = find_device(device, devices)
    if not matches:
        unknown_device(device, devices)
    return {'window': window, 'devices': [describe(d, window) for d in matches]}


def get_recent_blocks(device=None, limit=5):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ToolError('limit must be an integer from 1 to 20')
    device = device_filter(device)
    path = f'/events?action=block&limit={limit}'
    if device is not None:
        devices = read('/summary?window=24h').get('devices', [])
        matches = find_device(device, devices)
        if not matches:
            unknown_device(device, devices)
        rows = [row for d in matches
                for row in read(path + '&device=' + urllib.parse.quote(d['device']))]
        rows = sorted(rows, key=lambda row: row['ts'])[-limit:]
    else:
        rows = read(path)
    now = time.time() * 1000
    return {'blocks': [{'device': row['device'], 'domain': row['domain'], 'company': row.get('company'),
                        'reason': row.get('reason'), 'minutes_ago': int((now - row['ts']) // 60000)}
                       for row in reversed(rows)]}


def explain_domain(domain):
    if not isinstance(domain, str):
        raise ToolError('domain must be a string')
    domain = domain.strip().lower().rstrip('.')
    if len(domain) > 253 or not DOMAIN.fullmatch(domain):
        raise ToolError('domain must be a domain name like example.com')
    last = read('/events?limit=1&domain=' + urllib.parse.quote(domain))
    verdict = next((v for v in read('/ai/verdicts') if v.get('domain') == domain), None)
    if not last and not verdict:
        return {'domain': domain, 'seen': False}
    result = {'domain': domain, 'seen': bool(last)}
    if last:
        row = last[-1]
        result.update(company=row.get('company'), category=row.get('category'), last_decision={
            'action': row['action'], 'source': row.get('source'), 'reason': row.get('reason'),
            'device': row['device'], 'minutes_ago': int((time.time() * 1000 - row['ts']) // 60000)})
    if verdict:
        result['ai_verdict'] = {key: verdict.get(key) for key in ('label', 'confidence', 'reason')}
    return result


def what_they_know(hours=24):
    if type(hours) is not int or not 1 <= hours <= 168:
        raise ToolError('hours must be an integer from 1 to 168')
    return read(f'/what-they-know?hours={hours}')


HANDLERS = {'get_summary': get_summary, 'get_recent_blocks': get_recent_blocks,
            'explain_domain': explain_domain, 'what_they_know': what_they_know}

NUMBER_WORDS = {'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
                'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
                'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20, 'thirty': 30,
                'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90, 'hundred': 100}


def numbers_in(value, found=None):
    """Every number in a tool result, including digits inside strings; fractions also as percents."""
    found = set() if found is None else found
    if isinstance(value, bool):
        return found
    if isinstance(value, (int, float)):
        found.add(round(float(value), 2))
        if 0 < value < 1:
            found.add(float(round(value * 100)))
    elif isinstance(value, str):
        found.update(float(n) for n in re.findall(r'\d+(?:\.\d+)?', value))
    elif isinstance(value, dict):
        for item in value.values():
            numbers_in(item, found)
    elif isinstance(value, list):
        for item in value:
            numbers_in(item, found)
    return found


def reply_numbers(text):
    found = {float(n.replace(',', '')) for n in re.findall(r'\d[\d,]*(?:\.\d+)?', text)}
    found.update(float(NUMBER_WORDS[w]) for w in re.findall(r'[a-z]+', text.lower()) if w in NUMBER_WORDS)
    return found


class Agent:
    def __init__(self, url=None, model=None, chat=None, clock=time.monotonic):
        self.url = url or os.environ.get('EDDIE_OLLAMA_URL', DEFAULT_URL)
        self.model = model or os.environ.get('EDDIE_AGENT_MODEL', DEFAULT_MODEL)
        check_local(self.url)
        self.chat = chat or self.ollama_chat
        self.clock = clock

    def ollama_chat(self, messages, timeout):
        body = {'model': self.model, 'stream': False, 'messages': messages, 'tools': TOOLS,
                'options': {'temperature': 0, 'num_predict': 200}}
        request = urllib.request.Request(self.url.rstrip('/') + '/api/chat', data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'})
        with _OPENER.open(request, timeout=timeout) as response:
            return json.load(response)['message']

    def ask(self, text):
        """The spoken answer. Raises AgentUnavailable when the model can't produce one."""
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': text}]
        results = []
        deadline = self.clock() + DEADLINE
        for _ in range(MAX_ROUNDS):
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            try:
                message = self.chat(messages, min(CALL_TIMEOUT, remaining))
            except Exception as error:
                raise AgentUnavailable(str(error)) from error
            if not isinstance(message, dict):
                raise AgentUnavailable('malformed model reply')
            calls = message.get('tool_calls') or []
            if not calls:
                return self.checked(str(message.get('content') or ''), results)
            messages.append({'role': 'assistant', 'content': message.get('content') or '', 'tool_calls': calls})
            for call in calls:
                name, output = self.run_tool(call)
                if not isinstance(output, dict) or 'error' not in output:
                    results.append(output)
                messages.append({'role': 'tool', 'tool_name': name, 'content': json.dumps(output)})
        raise AgentUnavailable('no answer within the round limit')

    def run_tool(self, call):
        """(name, result) for one tool call. BackendError propagates and ends the turn."""
        fn = call.get('function') if isinstance(call, dict) else None
        name = fn.get('name') if isinstance(fn, dict) else None
        args = fn.get('arguments') if isinstance(fn, dict) else None
        if isinstance(args, str):
            try:
                args = json.loads(args or '{}')
            except ValueError:
                args = None
        if name not in HANDLERS:
            return str(name), {'error': f'Unknown tool. Available: {", ".join(HANDLERS)}.'}
        if not isinstance(args, dict):
            return name, {'error': 'Arguments must be a JSON object.'}
        try:
            return name, HANDLERS[name](**args)
        except (ToolError, TypeError) as error:
            detail = error.args[0] if error.args else ''
            return name, detail if isinstance(detail, dict) else {'error': str(detail)}

    def checked(self, reply, results):
        """The reply if every number in it came from a tool result, else backend text."""
        reply = ' '.join(reply.split())[:MAX_REPLY_CHARS]
        if reply and reply_numbers(reply) <= numbers_in(results):
            return reply
        for result in reversed(results):
            text = result.get('text') or result.get('spoken_summary_last_hour')
            if isinstance(text, str) and text:
                return text
        return UNVERIFIED


def answer(agent, text):
    """Spoken reply, or None when the model is unavailable and the caller should fall back."""
    try:
        return agent.ask(text)
    except BackendError:
        return BACKEND_FAILURE
    except AgentUnavailable:
        return None


if __name__ == '__main__':
    import sys
    reply = answer(Agent(), ' '.join(sys.argv[1:]) or "What's been going on?")
    print(reply if reply is not None else 'The local model is unavailable.')
