"""Local Ollama tool-calling agent: read-only traffic tools plus write tools
behind the spoken yes/no confirmation.

The model only phrases answers; every fact comes from the local Eddie API
through the tools below. The model can't change a rule, mode or device itself:
a write tool call (block, allow, lockdown_device, set_mode, #108) is checked
against WRITE_TOOLS' schema, turned into a read-back of exactly what would
change, and parked in voice_agent.confirmation. Only a yes runs it, through the
same actions as spoken commands (rules tagged created_by: voice, undoable).

- EDDIE_OLLAMA_URL must be localhost or a private IP literal; proxies and
  redirects are ignored, so questions and traffic facts stay in the house.
- Every number in the reply must appear in a tool result, otherwise the reply
  is replaced by backend text or an honest "can't verify".
- A backend failure ends the turn with a fixed spoken failure; a model failure
  raises AgentUnavailable so the caller can use the deterministic router.
- One write per question. A malformed, unknown-target or essentials-blocking
  write call ends the turn with "nothing changed"; the model never retries it.
- Questions, tool results and replies are never logged or stored.
- phrase() turns "what they know" facts into at most 5 "someone could guess"
  lines. The model sees only the computed facts, never raw logs; a line is
  dropped unless every number, clock time and weekday in it is in the facts.

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
    'Use tools for facts; never invent numbers. '
    'To block or allow a domain or service, lock a device down to some services, or turn home mode on or off, '
    'call that one tool once; Eddie reads it back and waits for a yes, so never say a change is done. '
    'To stop recording or listening, call stop_microphone. '
    'Use the device names the tools return. If a tool reports an error, say plainly what you could not find. '
    'Only state facts that appear in tool results; when a result has a "said" sentence, base your answer on it. '
    'If a tool says enough_data is false, say there is not enough data instead of guessing.'
)
BACKEND_FAILURE = "I can't reach Eddie's traffic backend, so I can't answer that right now."
UNVERIFIED = "I couldn't check that answer against Eddie's records, so I won't guess."
INVALID_WRITE = "I couldn't turn that into a change I can check, so nothing changed."
ONE_WRITE = "I can only set up one change at a time. Ask for them one by one. Nothing changed."
NOT_DONE = "I haven't changed anything. Ask me again and I'll read the change back for a yes."
# A final reply that claims a change; changes only happen after a yes, never inside ask.
CLAIMED = re.compile(r"\b(?:done|i(?:'ve| have) (?:just )?(?:blocked|allowed|locked|turned|set|added|changed)"
                     r"|(?:is|are) now (?:blocked|allowed|locked|on|off))\b")
# README "Intelligence features", step 5.
INFER_PROMPT = (
    'Using only these facts, write at most 5 things someone could infer about the people in this household: '
    'their schedule, sleep, when the house is empty, what they watch, which company profiles them. '
    'One per line, each as "Someone could guess <inference>, because <evidence copied from the fact>. '
    '<Fairly sure | Somewhat sure | Just a hunch>." Example: "Someone could guess you sleep late on weekends, '
    'because 12 streaming sessions started after 10 AM on Saturday. Somewhat sure." '
    'Never add facts, numbers, times or days that are not in the facts. No introduction, no list numbers.'
)
MAX_INFERENCES = 5
MAX_LINE_CHARS = 220
WEEKDAY = re.compile(r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b')
CLOCK = re.compile(r'\b\d{1,2}:\d{2}\b')


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


SERVICE_KEYS = list(voice_agent.SERVICES)
WRITE_TOOLS = [
    function('block', 'Block one exact domain on every device, after the user says yes.',
             {'domain': {'type': 'string', 'maxLength': 253}}, ['domain']),
    function('allow', 'Allow a known service or one exact domain on every device, after the user says yes. '
             'Give exactly one of service or domain.',
             {'service': {'type': 'string', 'enum': SERVICE_KEYS},
              'domain': {'type': 'string', 'maxLength': 253}}),
    function('lockdown_device', 'Block everything for one device except the keep services and Eddie\'s '
             'essentials, after the user says yes. "The TV can only talk to Netflix" is device "TV", '
             'keep ["netflix"].',
             {'device': {'type': 'string', 'maxLength': 64, 'description': 'The device\'s dashboard label.'},
              'keep': {'type': 'array', 'items': {'type': 'string', 'enum': SERVICE_KEYS},
                       'maxItems': len(SERVICE_KEYS), 'uniqueItems': True}},
             ['device', 'keep']),
    function('set_mode', 'Turn home mode on or off, after the user says yes.',
             {'home_mode': {'type': 'boolean'}}, ['home_mode']),
]
WRITE_SCHEMAS = {tool['function']['name']: tool['function']['parameters'] for tool in WRITE_TOOLS}

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
    voice_agent.BURGLARS_EYE_TOOL,
    voice_agent.STOP_MIC_TOOL,
] + WRITE_TOOLS


def valid(value, schema):
    """True if value matches the subset of JSON schema WRITE_TOOLS uses, exactly (no coercion)."""
    kind = schema['type']
    if kind == 'boolean':
        return type(value) is bool
    if kind == 'string':
        return (type(value) is str and len(value) <= schema.get('maxLength', len(value))
                and ('enum' not in schema or value in schema['enum']))
    if kind == 'array':
        return (type(value) is list and len(value) <= schema['maxItems']
                and len(set(map(str, value))) == len(value) and all(valid(v, schema['items']) for v in value))
    if kind == 'object':
        return (type(value) is dict and set(value) <= set(schema['properties'])
                and set(schema['required']) <= set(value)
                and all(valid(v, schema['properties'][k]) for k, v in value.items()))
    return False


def exact_domain(value):
    """A spoken or typed domain as POST /rules takes it (no wildcard), else None."""
    domain = value.strip().lower().rstrip('.')
    return domain if len(domain) <= 253 and voice_agent.DOMAIN.fullmatch(domain) else None


def propose(name, args):
    """(kind, args, prompt) for voice_agent.confirmation, or the spoken reply when nothing
    can be proposed. args already matched WRITE_SCHEMAS[name]."""
    if name == 'set_mode':
        state = 'on' if args['home_mode'] else 'off'
        return 'home_mode', {'enabled': args['home_mode']}, f'Turn home mode {state}?'
    if name == 'lockdown_device':
        spoken = voice_agent.words(args['device'])
        article = voice_agent.LOCKDOWN_ARTICLE.match(spoken + ' ')
        spoken = spoken[article.end():] if article else spoken
        if not spoken:
            return INVALID_WRITE
        return voice_agent.lockdown_request(article[0] if article else '', spoken, args['keep'])
    if name == 'allow' and len(args) != 1:
        return INVALID_WRITE
    if 'service' in args:
        return voice_agent.allow_request('service', args['service'])
    domain = exact_domain(args['domain'])
    if domain is None:
        return INVALID_WRITE
    if name == 'allow':
        return ('allow', {'name': domain, 'patterns': [domain]},
                f'Allow {domain} on every device? That adds one allow rule.')
    if voice_agent.essential(domain):
        return voice_agent.essential_reply(domain)
    return 'block', {'domain': domain}, f'Block {domain} on every device? That adds one block rule.'



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
    blocks = [{'device': row['device'], 'domain': row['domain'], 'company': row.get('company'),
                        'reason': row.get('reason'), 'minutes_ago': int((now - row['ts']) // 60000)}
              for row in reversed(rows)]
    if not blocks:
        said = 'Eddie has no recent blocks to report.'
    else:
        items = [f"{row['domain']} on {row['device']}" for row in blocks]
        said = 'Recent blocks were ' + ', '.join(items[:-1])
        said += ((' and ' if len(items) > 1 else '') + items[-1] + '.')
    return {'blocks': blocks, 'said': said}


def explain_domain(domain):
    if not isinstance(domain, str):
        raise ToolError('domain must be a string')
    domain = domain.strip().lower().rstrip('.')
    if len(domain) > 253 or not DOMAIN.fullmatch(domain):
        raise ToolError('domain must be a domain name like example.com')
    last = read('/events?limit=1&domain=' + urllib.parse.quote(domain))
    verdict = next((v for v in read('/ai/verdicts') if v.get('domain') == domain), None)
    if not last and not verdict:
        return {'domain': domain, 'seen': False, 'said': f"I haven't seen {domain}, so I can't say why it was blocked."}
    result = {'domain': domain, 'seen': bool(last)}
    if last:
        row = last[-1]
        result.update(company=row.get('company'), category=row.get('category'), last_decision={
            'action': row['action'], 'source': row.get('source'), 'reason': row.get('reason'),
            'device': row['device'], 'minutes_ago': int((time.time() * 1000 - row['ts']) // 60000)})
    if verdict:
        result['ai_verdict'] = {key: verdict.get(key) for key in ('label', 'confidence', 'reason')}
    if last:
        decision = result['last_decision']
        if decision['action'] == 'block':
            reason = decision.get('reason') or decision.get('source') or 'a local blocking rule'
            result['said'] = f"Eddie blocked {domain} on {decision['device']} because {reason}."
        else:
            result['said'] = f"The latest decision for {domain} was {decision['action']}, not block."
    else:
        reason = verdict.get('reason') or verdict.get('label')
        result['said'] = f"Eddie's local classifier flagged {domain} because {reason}."
    return result


def what_they_know(hours=None):
    if hours is None:
        return read('/what-they-know')
    if type(hours) is not int or not 1 <= hours <= 8760:
        raise ToolError('hours must be an integer from 1 to 8760')
    return read(f'/what-they-know?hours={hours}')


def burglars_eye(hours=None):
    if hours is None:
        return read('/burglars-eye')
    if type(hours) is not int or not 1 <= hours <= 8760:
        raise ToolError('hours must be an integer from 1 to 8760')
    return read(f'/burglars-eye?hours={hours}')


HANDLERS = {'get_summary': get_summary, 'get_recent_blocks': get_recent_blocks,
            'explain_domain': explain_domain, 'what_they_know': what_they_know,
            'burglars_eye': burglars_eye}
# Each tool's Guest Mode permission category (#163), checked before it runs. Write tools
# are checked by voice_agent.confirmation.request.
TOOL_CATEGORIES = {'get_summary': 'traffic', 'get_recent_blocks': 'traffic', 'explain_domain': 'traffic',
                   'what_they_know': 'reports', 'burglars_eye': 'reports', 'stop_microphone': 'microphone_off'}

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


def supported(line, facts):
    """True if every number, clock time and weekday in line appears in the facts."""
    text = json.dumps(facts).lower()
    return (reply_numbers(line) <= numbers_in(facts)
            and all(t in text for t in CLOCK.findall(line))
            and all(re.search(rf'\b{day[:3]}', text) for day in WEEKDAY.findall(line.lower())))  # facts may say "Tue"


class Agent:
    def __init__(self, url=None, model=None, chat=None, clock=time.monotonic, confirmation=None):
        self.url = url or os.environ.get('EDDIE_OLLAMA_URL', DEFAULT_URL)
        self.model = model or os.environ.get('EDDIE_AGENT_MODEL', DEFAULT_MODEL)
        check_local(self.url)
        self.chat = chat or self.ollama_chat
        self.clock = clock
        self.confirmation = confirmation or voice_agent.confirmation

    def ollama_chat(self, messages, timeout, tools=TOOLS, num_predict=200):
        body = {'model': self.model, 'stream': False, 'messages': messages, 'tools': tools,
                'options': {'temperature': 0, 'num_predict': num_predict}}
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
            if any(tool_name(call) == 'stop_microphone' for call in calls):
                return voice_agent.profile.check('microphone_off') or voice_agent.stop_microphone()
            writes = [call for call in calls if tool_name(call) in WRITE_SCHEMAS]
            if writes:
                return self.write(writes)
            messages.append({'role': 'assistant', 'content': message.get('content') or '', 'tool_calls': calls})
            for call in calls:
                name, output = self.run_tool(call)
                if not isinstance(output, dict) or 'error' not in output:
                    results.append(output)
                messages.append({'role': 'tool', 'tool_name': name, 'content': json.dumps(output)})
        raise AgentUnavailable('no answer within the round limit')

    def infer(self, facts):
        """Checked "someone could guess" lines from facts alone (no tools). Raises AgentUnavailable."""
        messages = [{'role': 'system', 'content': INFER_PROMPT}, {'role': 'user', 'content': json.dumps(facts)}]
        try:
            message = self.chat(messages, CALL_TIMEOUT, [], 400)
        except Exception as error:
            raise AgentUnavailable(str(error)) from error
        if not isinstance(message, dict):
            raise AgentUnavailable('malformed model reply')
        lines = []
        for line in str(message.get('content') or '').splitlines():
            line = ' '.join(re.sub(r'^\s*(?:[-*•]|\d+[.)])\s*', '', line).replace('**', '').split())
            if (line.lower().startswith('someone could guess') and len(line) <= MAX_LINE_CHARS
                    and supported(line, facts)):
                lines.append(line)
        return lines[:MAX_INFERENCES]

    def write(self, calls):
        """The read-back for one write call, now pending in confirmation, or why nothing changed."""
        if len(calls) > 1:
            return ONE_WRITE
        name, args = tool_name(calls[0]), tool_args(calls[0])
        if not valid(args, WRITE_SCHEMAS[name]):
            return INVALID_WRITE
        proposal = propose(name, args)
        if isinstance(proposal, str):
            return proposal
        return self.confirmation.request(*proposal)

    def run_tool(self, call):
        """(name, result) for one tool call. BackendError propagates and ends the turn."""
        name, args = tool_name(call), tool_args(call)
        if name not in HANDLERS:
            return str(name), {'error': f'Unknown tool. Available: {", ".join(HANDLERS)}.'}
        denied = voice_agent.profile.check(TOOL_CATEGORIES[name])
        if denied:
            return name, {'error': denied}
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
        if CLAIMED.search(reply.lower()):
            return NOT_DONE
        if reply and reply_numbers(reply) <= numbers_in(results):
            return reply
        for result in reversed(results):
            text = result.get('text') or result.get('spoken_summary_last_hour')
            if isinstance(text, str) and text:
                return text
        return UNVERIFIED


def tool_name(call):
    fn = call.get('function') if isinstance(call, dict) else None
    return fn.get('name') if isinstance(fn, dict) else None


def tool_args(call):
    """The call's arguments as a dict, or None; Ollama may send them as a JSON string."""
    fn = call.get('function') if isinstance(call, dict) else None
    args = fn.get('arguments') if isinstance(fn, dict) else None
    if isinstance(args, str):
        try:
            args = json.loads(args or '{}')
        except ValueError:
            return None
    return args


def answer(agent, text):
    """Spoken reply, or None when the model is unavailable and the caller should fall back."""
    try:
        return agent.ask(text)
    except BackendError:
        return BACKEND_FAILURE
    except AgentUnavailable:
        return None


def phrase(agent, facts):
    """Model-written inferences from computed facts, or None to keep the backend's template."""
    if not facts:
        return None
    try:
        lines = agent.infer(facts)
    except AgentUnavailable:
        return None
    return ' '.join(lines) or None


if __name__ == '__main__':
    import sys
    reply = answer(Agent(), ' '.join(sys.argv[1:]) or "What's been going on?")
    print(reply if reply is not None else 'The local model is unavailable.')
