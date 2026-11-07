"""Small deterministic tool router; never claims unavailable DNS actions succeeded."""
import json
import os
import re
import threading
import time
import urllib.request


def request(path, body=None):
    base = os.environ.get('EDDIE_API_URL', 'http://127.0.0.1:8000').rstrip('/')
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=3) as response:
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


CONFIRM_SECONDS = 15
YES = {'yes', 'yeah', 'yep', 'yes please', 'do it', 'yes do it', 'go ahead', 'confirm'}
NO = {'no', 'nope', 'no thanks', 'cancel', 'cancel it', 'stop', 'dont', 'never mind'}
# Risky actions run only from Confirmation.reply after an explicit yes. Each maps
# kind -> callable(**args) returning the spoken result only after backend
# acknowledgment. RISKY_COMMANDS parse normalized text -> (kind, args, prompt).
# Both stay empty until lockdown and email exist.
RISKY_ACTIONS = {}
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


def respond(text):
    raw = text.strip()
    text = re.sub(r'[^a-z0-9\s]', '', text.lower()).strip()
    text = re.sub(r'\s+', ' ', text)
    # A new risky command replaces any pending one; otherwise a pending action
    # consumes the reply, including missed speech.
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
    if re.search(r'\b(block|allow|lock|undo|email|send|mode)\b', text):
        return "I haven't changed anything. I can turn home mode on or off; domain rules, lockdown and email aren't connected yet."
    if re.search(r'\bwhat (?:do|does|can|could) (?:\w+|my \w+|the \w+) (?:know|tell|guess|figure out|learn) about (?:me|us|my house|my home|our house)\b', text):
        try:
            return str(what_they_know()['text'])
        except Exception:
            return "I can't reach Eddie’s traffic backend, so I can't say what they know right now."
    if text in ('hello', 'hi', 'hey', 'hey eddie'):
        return "Hey, I'm Eddie. Ask me what's been going on, or tell me to turn home mode on or off."
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
