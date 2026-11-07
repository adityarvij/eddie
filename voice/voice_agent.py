"""Small deterministic tool router; never claims unavailable DNS actions succeeded."""
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path


def request(path, body=None, timeout=3):
    base = os.environ.get('EDDIE_API_URL', 'http://127.0.0.1:8000').rstrip('/')
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


HOME_MODE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'set_home_mode',
        'description': 'Enable or disable home mode when the user explicitly requests it.',
        'parameters': {
            'type': 'object',
            'properties': {'enabled': {'type': 'boolean'}},
            'required': ['enabled'],
            'additionalProperties': False,
        },
    },
}


def set_home_mode(enabled):
    """Shared action for the voice router and future local-model tool calling."""
    if type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    result = request('/mode', {'home_mode': enabled})
    if result.get('home_mode') is not enabled:
        raise ValueError('Mode was not acknowledged')
    return {'home_mode': enabled}


WHAT_THEY_KNOW_TOOL = {
    'type': 'function',
    'function': {
        'name': 'what_they_know',
        'description': 'Read what companies could infer about the household from recent device traffic.',
        'parameters': {
            'type': 'object',
            'properties': {'hours': {'type': 'integer', 'minimum': 1, 'maximum': 168}},
            'additionalProperties': False,
        },
    },
}


def what_they_know(hours=24):
    """Backend-computed inferences only; the reply text comes from /what-they-know."""
    if type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError('hours must be an integer from 1 to 168')
    return request(f'/what-they-know?hours={hours}')


# A domain as POST /rules accepts it (no wildcard), and a domain inside a spoken reply.
DOMAIN = re.compile(r'[a-z0-9_-]+(?:\.[a-z0-9_-]+)+')
SPOKEN_DOMAIN = re.compile(r'\b[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.[a-z]{2,}\b')
# eddie/labels.py TRACKING; copied so the voice worker doesn't import the backend.
TRACKING = {'telemetry', 'ads', 'analytics', 'tracking'}
IT_WINDOW_MS = 15 * 60 * 1000  # "it" never reaches back further than this in /events
RULE_TIMEOUT = 5  # POST /rules waits up to 2 s for the DNS engine before answering
IT_WORDS = r'it|that|this|that one|this one|that domain|this domain|that tracker|this tracker'
WILDCARD = re.compile(r'\*\.[a-z0-9_-]+(?:\.[a-z0-9_-]+)+')


def _command(verb):
    return re.compile(
        r'(?:(?:ok|okay|eddie|please) )*(?:(?:can|could|would|will) you (?:please )?)?(?:go ahead and )?'
        rf'{verb} (?P<target>.+?)(?: (?:please|for me|now|too|as well))*')


BLOCK = _command('block')
ALLOW = _command('allow')
NOT_RESOLVED = 'Which domain should I block? Say block, then the full domain name. Nothing changed.'
RULE_FAILED = "I couldn't add that rule. Nothing changed."
# Spoken service name -> patterns for "allow <service>"; one file, covered by tests/test_voice_allow.py.
SERVICES = {key: value for key, value in json.loads(
    (Path(__file__).with_name('services.json')).read_text()).items() if not key.startswith('_')}
SERVICE_ALIASES = {alias: key for key, service in SERVICES.items() for alias in service['aliases']}
_names = [service['name'] for service in SERVICES.values()]
ALLOW_UNRESOLVED = (f"Which service or domain should I allow? I know {', '.join(_names[:-1])} and {_names[-1]}, "
                    "or say allow, then the full domain name. Nothing changed.")
# The last domain Eddie spoke about in this conversation; memory only, cleared by forget_conversation.
context = {'domain': None}


def command_target(raw, command):
    """The target of a whole-utterance command (BLOCK or ALLOW), else None.

    Spoken "dot" becomes "." because transcripts spell domains out.
    """
    text = raw.lower().replace("'", '').replace('’', '')
    text = re.sub(r'\s+dot\s+', '.', text)
    text = ' '.join(re.sub(r'[^a-z0-9._*-]+', ' ', text).split()).strip('.')
    match = command.fullmatch(text)
    if not match or re.search(r'\b(?:and|but|then)\b', match['target']):
        return None  # extra requests get the "nothing changed" reply
    return re.sub(r'^(?:the )', '', match['target']).rstrip('.')


def block_target(raw):
    """(kind, domain) for a whole-utterance block command, else None.

    kind is 'it' (domain None) or 'domain' (domain None when it isn't a domain name).
    """
    target = command_target(raw, BLOCK)
    if target is None:
        return None
    if re.fullmatch(IT_WORDS, target):
        return 'it', None
    return 'domain', target if len(target) <= 253 and DOMAIN.fullmatch(target) else None


def resolve_it():
    """The domain "it" means: the last one Eddie spoke about, else the newest flagged or
    allowed tracker in the last 15 minutes of /events. None if neither exists."""
    if context['domain']:
        return context['domain']
    since = int(time.time() * 1000) - IT_WINDOW_MS
    for row in reversed(request(f'/events?since={since}')):
        tracker_like = row.get('action') == 'flag' or (
            row.get('action') == 'allow' and row.get('category') in TRACKING)
        if tracker_like and DOMAIN.fullmatch(str(row.get('domain', ''))):
            return row['domain']
    return None


def add_rule(pattern, action):
    """POST a global rule; returns applied, raising unless the backend stored this rule."""
    result = request('/rules', {'pattern': pattern, 'action': action, 'created_by': 'voice'},
                     timeout=RULE_TIMEOUT)
    if (type(result.get('id')) is not int or result.get('pattern') != pattern
            or result.get('action') != action):
        raise ValueError('Rule was not acknowledged')
    return result.get('applied') is True


def block_domain(domain):
    return add_rule(domain, 'block')


def block_command(kind, domain):
    if kind == 'it':
        try:
            domain = resolve_it()
        except Exception:
            return "I can't reach Eddie’s traffic backend, so I don't know what it is. Nothing changed."
    if domain is None:
        return NOT_RESOLVED
    try:
        applied = block_domain(domain)
    except Exception:
        return RULE_FAILED
    if applied:
        return f"Blocked {domain} on every device. The rule is saved and Eddie's DNS has loaded it."
    return (f"I saved a rule to block {domain}, but Eddie's DNS hasn't loaded it yet, "
            "so it may still get through.")


def allow_target(raw):
    """(kind, value) for a whole-utterance allow command, else None.

    kind is 'service' (a SERVICES key), 'domain' (one exact host), 'wildcard'
    (a typed *.a.com) or 'unknown' (value None: ask, never guess a pattern).
    """
    target = command_target(raw, ALLOW)
    if target is None:
        return None
    service = SERVICE_ALIASES.get(re.sub(r'[^a-z0-9]+', ' ', target).strip())
    if service:
        return 'service', service
    if len(target) <= 253 and DOMAIN.fullmatch(target):
        return 'domain', target
    if len(target) <= 253 and WILDCARD.fullmatch(target):
        return 'wildcard', target
    return 'unknown', None


def allow_request(kind, value):
    """Confirmation.request args for a broad allow (a service or a wildcard)."""
    if kind == 'service':
        service = SERVICES[value]
        name, patterns = service['name'], service['patterns']
        prompt = f'Allow {name} on every device? That adds {len(patterns)} allow rules for its domains.'
    else:
        name, patterns = f'{value[2:]} and every subdomain', [value]
        prompt = f'Allow {name} on every device?'
    return 'allow', {'name': name, 'patterns': patterns}, prompt


def allow_patterns(name, patterns):
    """RISKY_ACTIONS['allow']: add each allow rule in order, stopping at the first failure.

    The last rule's applied covers the earlier ones: the DNS engine loads whole revisions.
    """
    saved, applied = 0, False
    for pattern in patterns:
        try:
            applied = add_rule(pattern, 'allow')
        except Exception:
            break
        saved += 1
    if saved == 0:
        return RULE_FAILED
    if saved < len(patterns):
        return (f"I only saved {saved} of {len(patterns)} allow rules for {name} before the backend "
                f"stopped answering, so {name} may still be blocked. The saved rules stay.")
    if not applied:
        return f"I saved the allow rules for {name}, but Eddie's DNS hasn't loaded them yet, so it may still be blocked."
    loaded = ("The rule is saved and Eddie's DNS has loaded it." if saved == 1
              else "The rules are saved and Eddie's DNS has loaded them.")
    return f'Allowed {name} on every device. {loaded}'


def allow_command(kind, value):
    """The reply to an allow that needs no confirmation: one exact domain, or an unknown target."""
    if kind == 'unknown':
        return ALLOW_UNRESOLVED
    try:
        applied = add_rule(value, 'allow')
    except Exception:
        return RULE_FAILED
    if applied:
        return f"Allowed {value} on every device. The rule is saved and Eddie's DNS has loaded it."
    return f"I saved a rule to allow {value}, but Eddie's DNS hasn't loaded it yet, so it may still be blocked."


def forget_conversation():
    """Drop the remembered domain when the conversation or microphone session ends."""
    context['domain'] = None


CONFIRM_SECONDS = 15
YES = {'yes', 'yeah', 'yep', 'yes please', 'do it', 'yes do it', 'go ahead', 'confirm'}
NO = {'no', 'nope', 'no thanks', 'cancel', 'cancel it', 'stop', 'dont', 'never mind'}
# Risky actions run only from Confirmation.reply after an explicit yes. Each maps
# kind -> callable(**args) returning the spoken result only after backend
# acknowledgment. RISKY_COMMANDS parse normalized text -> (kind, args, prompt).
# Broad allows (a service or a wildcard) are parsed in route, not RISKY_COMMANDS,
# because they need the raw text's dots.
RISKY_ACTIONS = {'allow': allow_patterns}
RISKY_COMMANDS = []


class Confirmation:
    """One pending risky action. Ambiguous replies and timeouts never execute."""
    def __init__(self, clock=time.monotonic, seconds=CONFIRM_SECONDS):
        self.clock = clock
        self.seconds = seconds
        self.pending = None
        self.lock = threading.Lock()

    def request(self, kind, args, prompt):
        if kind not in RISKY_ACTIONS:
            raise ValueError('Unknown risky action')
        with self.lock:
            replaced = self.pending is not None
            self.pending = {'kind': kind, 'args': dict(args), 'prompt': prompt,
                            'expires_at': self.clock() + self.seconds, 'reprompted': False}
        lead = "Okay, I dropped the earlier request. " if replaced else ''
        return f'{lead}{prompt} Say yes to confirm or no to cancel.'

    def status(self):
        """Screen-safe view of the pending action; arguments are never exposed."""
        with self.lock:
            pending = self.pending
            remaining = pending and pending['expires_at'] - self.clock()
            if not pending or remaining <= 0:
                return None
            return {'kind': pending['kind'], 'prompt': pending['prompt'], 'expires_in_ms': int(remaining * 1000)}

    def cancel(self):
        with self.lock:
            self.pending = None

    def reply(self, text):
        """Spoken reply when a pending action consumes normalized text, else None."""
        with self.lock:
            pending = self.pending
            if pending is None:
                return None
            if self.clock() >= pending['expires_at']:
                self.pending = None
                if text in YES or text in NO:
                    return "That request expired, so I didn't do it. Ask again if you still want it."
                return None
            if text in NO:
                self.pending = None
                return 'Cancelled. Nothing changed.'
            if text not in YES:
                if pending['reprompted']:
                    self.pending = None
                    return "I didn't get a clear yes, so I didn't do it."
                pending['reprompted'] = True
                return f"I need a yes or no. {pending['prompt']}"
            self.pending = None
        try:
            return str(RISKY_ACTIONS[pending['kind']](**pending['args']))
        except Exception:
            return "I couldn't confirm that went through. Check the local backend before trying again."


confirmation = Confirmation()
# Optional local model for read-only questions: callable(raw text) -> reply, or
# None when the model is unavailable. Set by frontend/serve.py (ollama_agent).
ask_model = None
# Optional local model that writes "what they know" inferences from the backend's
# facts: callable(facts) -> text, or None to keep the backend's template.
phrase_inferences = None
REPORT_HOURS = 168  # "what do they know about me" reads up to a week of traffic


def respond(text):
    reply = route(text)
    domains = SPOKEN_DOMAIN.findall(reply.lower())
    if domains:
        context['domain'] = domains[-1]
    return reply


def route(text):
    raw = text.strip()
    text = re.sub(r'[^a-z0-9\s]', '', text.lower()).strip()
    text = re.sub(r'\s+', ' ', text)
    # A new risky command replaces any pending one; otherwise a pending action
    # consumes the reply, including missed speech.
    allow = allow_target(raw)
    if allow and allow[0] in ('service', 'wildcard'):
        return confirmation.request(*allow_request(*allow))
    for parse in RISKY_COMMANDS:
        command = parse(text)
        if command:
            return confirmation.request(*command)
    pending_reply = confirmation.reply(text)
    if pending_reply is not None:
        return pending_reply
    if not text:
        return "Didn't catch that. Try again."
    # Match the entire request so negations, questions and additional actions
    # cannot accidentally activate a privacy setting.
    on = re.fullmatch(r'(?:please )?(?:(?:turn|switch) on home mode|(?:turn|switch) home mode on|enable home mode|(?:i want|id like) home mode (?:on|enabled)|(?:can|could|would) you (?:turn on|enable) home mode)(?: please)?', text)
    off = re.fullmatch(r'(?:please )?(?:(?:turn|switch) off home mode|(?:turn|switch) home mode off|disable home mode|(?:i want|id like) home mode (?:off|disabled)|(?:can|could|would) you (?:turn off|disable) home mode)(?: please)?', text)
    if on or off:
        enabled = bool(on)
        try:
            set_home_mode(enabled)
            return f"Home mode turned {'on' if enabled else 'off'}."
        except Exception:
            return "I couldn't update home mode. Check the local backend before trying again."
    block = block_target(raw)
    if block:
        return block_command(*block)
    if allow:
        return allow_command(*allow)
    if re.search(r'\b(block|allow|lock|undo|email|send|mode)\b', text):
        return "I haven't changed anything. I can block or allow a domain, allow a service like Netflix, or turn home mode on or off; undo, lockdown and email aren't connected yet."
    if re.search(r'\bwhat (?:do|does|can|could) (?:\w+|my \w+|the \w+) (?:know|tell|guess|figure out|learn) about (?:me|us|my house|my home|our house)\b', text):
        try:
            report = what_they_know(REPORT_HOURS)
        except Exception:
            return "I can't reach Eddie’s traffic backend, so I can't say what they know right now."
        if phrase_inferences is not None and report.get('facts'):
            reply = phrase_inferences(report['facts'])
            if reply:
                return reply
        return str(report['text'])
    if text in ('hello', 'hi', 'hey', 'hey eddie'):
        return "Hey, I'm Eddie. Ask me what's been going on, tell me to block a domain, or to turn home mode on or off."
    if ask_model is not None:
        reply = ask_model(raw)
        if reply is not None:
            return reply
    if re.search(r'\b(summary|update|happening|going|requests|blocked|doing|activity)\b', text):
        try:
            return str(request('/summary')['text'])
        except Exception:
            return "I can't reach Eddie’s traffic backend, so I don't have a current summary."
    return "Try asking what's been going on, or say turn on home mode."
