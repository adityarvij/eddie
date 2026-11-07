"""Small deterministic tool router; never claims unavailable DNS actions succeeded."""
import json
import os
import re
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


def respond(text):
    if not text.strip():
        return "Didn't catch that. Try again."
    text = re.sub(r'[^a-z0-9\s]', '', text.lower()).strip()
    text = re.sub(r'\s+', ' ', text)
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
    if text in ('hello', 'hi', 'hey', 'hey eddie'):
        return "Hey, I'm Eddie. Ask me what's been going on, or tell me to turn home mode on or off."
    if re.search(r'\b(summary|update|happening|going|requests|blocked|doing|activity)\b', text):
        try:
            return str(request('/summary')['text'])
        except Exception:
            return "I can't reach Eddie’s traffic backend, so I don't have a current summary."
    return "Try asking what's been going on, or say turn on home mode."
