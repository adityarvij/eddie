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


def respond(text):
    text = re.sub(r'[^a-z0-9\s]', '', text.lower()).strip()
    if re.fullmatch(r'(?:please )?(?:turn|switch) (on|off) home mode(?: please)?', text):
        enabled = 'on' in text.split()
        try:
            result = request('/mode', {'home_mode': enabled})
            if result.get('home_mode') is not enabled:
                raise ValueError('Mode was not acknowledged')
            return f"Home mode is {'on' if enabled else 'off'} in Eddie’s backend. DNS enforcement depends on the connected decision engine."
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
