"""Small deterministic tool router; never claims unavailable DNS actions succeeded."""
import functools
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path


def request(path, body=None, timeout=3, method=None):
    base = os.environ.get('EDDIE_API_URL', 'http://127.0.0.1:8000').rstrip('/')
    data = None if body is None else json.dumps(body).encode()
    headers = {'Content-Type': 'application/json'}
    if os.environ.get('EDDIE_API_TOKEN'):  # needed when the API isn't on this machine's loopback (#96)
        headers['Authorization'] = 'Bearer ' + os.environ['EDDIE_API_TOKEN']
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
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
    """Shared action for the voice router and the local model's set_mode tool."""
    if type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    result = request('/mode', {'home_mode': enabled})
    if result.get('home_mode') is not enabled:
        raise ValueError('Mode was not acknowledged')
    return {'home_mode': enabled}


def home_mode_action(enabled):
    """RISKY_ACTIONS['home_mode']: the model's set_mode after a yes. /mode only stores the
    flag; the DNS server reports nothing back, so never claim it's enforced."""
    try:
        set_home_mode(enabled)
    except Exception:
        return "I couldn't update home mode. Check the local backend before trying again."
    return (f"Home mode is saved as {'on' if enabled else 'off'}. Eddie's DNS doesn't report back on home mode, "
            "so I can't say it's being enforced.")


WHAT_THEY_KNOW_TOOL = {
    'type': 'function',
    'function': {
        'name': 'what_they_know',
        'description': 'Read what companies could infer about the household from recent device traffic.',
        'parameters': {
            'type': 'object',
            'properties': {'hours': {'type': 'integer', 'minimum': 1, 'maximum': 8760,
                                     'description': 'Requested lookback in hours. Omit to use all retained data.'}},
            'additionalProperties': False,
        },
    },
}


def what_they_know(hours=None):
    """Backend-computed inferences only; the reply text comes from /what-they-know."""
    if hours is None:
        return request('/what-they-know')
    if type(hours) is not int or not 1 <= hours <= 8760:
        raise ValueError('hours must be an integer from 1 to 8760')
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
# Rules this worker created, one entry per command, newest last:
# {'what': 'block on a.com', 'rules': [{'id', 'pattern', 'action', 'ts'}, ...]}.
# Memory only, kept until the worker restarts; only POST /rules acks from voice go in,
# so undo can't reach a dashboard or agent rule.
undo_stack = []
UNDO_DEPTH = 20
UNDO = re.compile(
    r'(?:(?:ok|okay|eddie|please|never mind|nevermind|actually|oops|wait) )*'
    r'(?:(?:can|could|would|will) you (?:please )?)?undo'
    r'(?: (?:it|that|this|that one|the last one|the last rule|the last change|that rule|that change|'
    r'that block|that allow|what you just did))?(?: (?:please|for me|now))*')
NOTHING_TO_UNDO = "There's nothing for me to undo."


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


def add_rule(pattern, action, saved=None, device=None):
    """POST a rule (global, or for one device key); returns applied, raising unless
    the backend stored this rule.

    The acknowledged rule is appended to saved, for remember_rules.
    """
    body = {'pattern': pattern, 'action': action, 'created_by': 'voice'}
    if device is not None:
        body['device_mac'] = device
    result = request('/rules', body, timeout=RULE_TIMEOUT)
    if (type(result.get('id')) is not int or result.get('pattern') != pattern
            or result.get('action') != action or result.get('device_mac') != device):
        raise ValueError('Rule was not acknowledged')
    if saved is not None:
        saved.append({'id': result['id'], 'pattern': pattern, 'action': action, 'ts': result.get('ts')})
    return result.get('applied') is True


def remember_rules(what, saved):
    """Push one command's acknowledged rules so "undo" can delete them."""
    if saved:
        undo_stack.append({'what': what, 'rules': saved})
        del undo_stack[:-UNDO_DEPTH]


ALLOWLIST = Path(__file__).resolve().parent.parent / 'allowlist.txt'


@functools.cache
def essentials():
    """The essentials allowlist (eddie/lists.py reads the same file), or None if unreadable."""
    try:
        lines = ALLOWLIST.read_text().splitlines()
    except OSError:
        return None
    return {line.split('#', 1)[0].strip().lower() for line in lines} - {''}


def essential(domain):
    """True if domain is an allowlist essential or under one. A global block rule beats the
    allowlist (decision.py), so voice never adds one there; an unreadable list counts as essential."""
    entries = essentials()
    labels = domain.split('.')
    return entries is None or any('.'.join(labels[i:]) in entries for i in range(len(labels) - 1))


def essential_reply(domain):
    return (f"{domain} is one of Eddie's essentials, like sign-in, updates and time, so I won't block it. "
            "Nothing changed.")


def block_domain(domain):
    saved = []
    try:
        return add_rule(domain, 'block', saved)
    finally:
        remember_rules(f'block on {domain}', saved)


def block_command(kind, domain):
    if kind == 'it':
        try:
            domain = resolve_it()
        except Exception:
            return "I can't reach Eddie’s traffic backend, so I don't know what it is. Nothing changed."
    if domain is None:
        return NOT_RESOLVED
    if essential(domain):
        return essential_reply(domain)
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
    rules, applied = [], False
    for pattern in patterns:
        try:
            applied = add_rule(pattern, 'allow', rules)
        except Exception:
            break
    remember_rules(f'allow rule for {name}' if len(rules) == 1 else f'allow rules for {name}', rules)
    saved = len(rules)
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
    saved = []
    try:
        applied = add_rule(value, 'allow', saved)
    except Exception:
        return RULE_FAILED
    finally:
        remember_rules(f'allow rule for {value}', saved)
    if applied:
        return f"Allowed {value} on every device. The rule is saved and Eddie's DNS has loaded it."
    return f"I saved a rule to allow {value}, but Eddie's DNS hasn't loaded it yet, so it may still be blocked."


LOCKDOWN = _command(r'lock ?down')
LOCKDOWN_ARTICLE = re.compile(r'(?:the|my|our) ')
# Services a locked-down device keeps by default (#17): every streaming service
# voice knows. The allowlist's essentials (sign-in, updates, time) survive anyway.
LOCKDOWN_KEEP = list(SERVICES)
LOCKDOWN_UNREACHABLE = "I can't reach Eddie's backend, so I can't look up that device. Nothing changed."


def words(text):
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text.lower().replace("'", '').replace('’', '')).split())


def lockdown_target(raw):
    """(article, spoken device) for a whole-utterance lockdown command, else None."""
    match = LOCKDOWN.fullmatch(words(raw))
    if not match or re.search(r'\b(?:and|but|then|except|to|only)\b', match['target']):
        return None  # extra requests and keep lists get the "nothing changed" reply
    target = match['target']
    article = LOCKDOWN_ARTICLE.match(target)
    return (article[0] if article else ''), target[article.end() if article else 0:]


def find_devices(spoken, devices):
    """The /devices rows whose label is the spoken name, else those whose label contains
    it as whole words. Only labels count: vendor guesses and IPs aren't names people say."""
    labelled = [d for d in devices if d.get('label') and words(d['label'])]
    exact = [d for d in labelled if words(d['label']) == spoken]
    return exact or [d for d in labelled if f' {spoken} ' in f" {words(d['label'])} "]


def lockdown_request(article, spoken, keep=LOCKDOWN_KEEP):
    """Confirmation.request args to lock a device down to the keep services (SERVICES
    keys), or the spoken reply when the device is unknown, ambiguous or already locked
    down (nothing changes then)."""
    try:
        devices = request('/devices')['devices']
    except Exception:
        return LOCKDOWN_UNREACHABLE
    found = find_devices(spoken, devices)
    if not found:
        names = sorted({d['label'] for d in devices if d.get('label')})
        known = f" The devices I know by name are {join(names)}." if names else ''
        return (f"I don't know a device called {spoken}.{known} Label it on the dashboard's device panel, "
                "then ask again. Nothing changed.")
    if len({d['mac'] for d in found}) > 1:
        return f"Which one: {join([d['label'] for d in found], 'or')}? Nothing changed."
    device = found[0]
    name = article + device['label']
    try:
        locked = any(r.get('pattern') == '*' and r.get('device_mac') == device['mac'] for r in request('/rules'))
    except Exception:
        return LOCKDOWN_UNREACHABLE
    if locked:
        return f'{name[0].upper()}{name[1:]} is already locked down. Nothing changed.'
    names = [SERVICES[key]['name'] for key in keep]
    reach = f"{join(names)}, plus Eddie's essentials" if names else "Eddie's essentials"
    prompt = f"Lock down {name}? It'll only reach {reach} like sign-in and updates."
    return 'lockdown', {'device': device['mac'], 'name': name, 'keep': list(keep)}, prompt


def block_action(domain):
    """RISKY_ACTIONS['block']: the model's block tool after a yes."""
    return block_command('domain', domain)


def join(names, last='and'):
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} {last} {names[-1]}"


def delete_rules(rules):
    """Delete acknowledged rules newest first, popping each one the backend removed
    (or no longer has). Stops at the first failure; what's left is still stored."""
    while rules:
        try:
            result = request(f"/rules/{rules[-1]['id']}", method='DELETE', timeout=RULE_TIMEOUT)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code != 404:
                return
        except Exception:
            return
        else:
            if result.get('deleted') is not True or result.get('id') != rules[-1]['id']:
                return
        rules.pop()


def lockdown_device(device, name, keep):
    """RISKY_ACTIONS['lockdown']: device allow rules for every keep pattern, then the
    device's "*" block rule last, so a device is never blocked without its keep list.

    On any failure the saved rules are deleted again; ones that can't be stay on
    undo_stack. Success means the rules are saved and loaded, never that traffic
    was seen blocked.
    """
    patterns = [pattern for key in keep for pattern in SERVICES[key]['patterns']]
    rules, applied = [], False
    try:
        for pattern in patterns:
            add_rule(pattern, 'allow', rules, device)
        applied = add_rule('*', 'block', rules, device)
    except Exception:
        saved = len(rules)
        delete_rules(rules)
        remember_rules(f'leftover lockdown rules for {name}', rules)
        if saved == 0:
            return f"I couldn't lock down {name}; the backend didn't save any rules. Nothing changed."
        if not rules:
            return (f"I only saved {saved} of {len(patterns) + 1} lockdown rules for {name} before the backend "
                    "stopped answering, so I removed them again. Nothing is locked down.")
        return (f"I only saved {saved} of {len(patterns) + 1} lockdown rules for {name}, and couldn't remove "
                f"{len(rules)} of them. Nothing is locked down, but those allow rules are still saved; "
                "say undo to remove them.")
    remember_rules(f'lockdown rules for {name}', rules)
    if not applied:
        return (f"I saved the lockdown rules for {name}, but Eddie's DNS hasn't loaded them yet, "
                "so nothing is blocked yet.")
    return (f"{name[0].upper()}{name[1:]} is locked down. The rules are saved and Eddie's DNS has loaded them. "
            "I haven't seen its traffic blocked yet, and it only applies while it uses Eddie for DNS.")


# "allow the new device" / "deny the new device" (#98): the newest device /devices
# lists as pending, read back with a yes before POST /devices/<mac>/approve|deny.
NEW_DEVICE = re.compile(
    r'(?:(?:ok|okay|eddie|please) )*(?:(?:can|could|would|will) you (?:please )?)?(?:go ahead and )?'
    r'(?P<verb>allow|approve|let in|deny|block|reject) (?:the |that |this )?new device(?: (?:please|for me|now))*')
NEW_DEVICE_STATUS = {'allow': 'approve', 'approve': 'approve', 'let in': 'approve',
                     'deny': 'deny', 'block': 'deny', 'reject': 'deny'}


def spoken_time(ms):
    """A first_seen time as people say it: 2:14 AM."""
    return time.strftime('%I:%M %p', time.localtime(ms / 1000)).lstrip('0')


def new_device_request(verb):
    """Confirmation.request args for approving or denying the newest pending device, or the
    spoken reply when there is none (nothing changes then)."""
    decision = NEW_DEVICE_STATUS[verb]
    try:
        devices = request('/devices')['devices']
    except Exception:
        return LOCKDOWN_UNREACHABLE
    pending = sorted((d for d in devices if d.get('status') == 'pending'),
                     key=lambda d: d.get('first_seen') or 0, reverse=True)
    if not pending:
        return "There's no new device waiting for a decision. Nothing changed."
    device = pending[0]
    seen = f", first seen at {spoken_time(device['first_seen'])}" if device.get('first_seen') else ''
    others = (f" {len(pending) - 1} other new device{'s are' if len(pending) > 2 else ' is'} still waiting."
              if len(pending) > 1 else '')
    if decision == 'approve':
        prompt = f"Allow the new device, {device['name']}{seen}? It'll get full internet through Eddie.{others}"
    else:
        prompt = (f"Deny the new device, {device['name']}{seen}? Eddie will block everything it looks up "
                  f"except the essentials, even with quarantine off.{others}")
    return decision, {'device': device['mac'], 'name': device['name']}, prompt


def decide_new_device(decision, device, name):
    """RISKY_ACTIONS['approve'] and ['deny']: success only once the backend stored the decision."""
    want = 'approved' if decision == 'approve' else 'denied'
    result = request(f'/devices/{device}/{decision}', {}, timeout=RULE_TIMEOUT)
    if result.get('mac') != device or result.get('status') != want:
        return f"I couldn't {decision} {name}; the backend didn't confirm it. Nothing changed."
    if result.get('applied') is not True:
        return f"I saved that {name} is {want}, but Eddie's DNS hasn't loaded it yet."
    if want == 'approved':
        return f"{name} is approved, and Eddie's DNS has loaded the change."
    return (f"{name} is denied, and Eddie's DNS has loaded the change. It only applies while "
            "the device uses Eddie for DNS.")


def still_voice_rule(row, rule):
    """True when a GET /rules row is the rule voice created, not a later rule that reused its id."""
    return bool(row) and row.get('created_by') == 'voice' and all(
        row.get(key) == rule[key] for key in ('pattern', 'action', 'ts'))


def undo_command():
    """Delete the rules of the newest voice command still on undo_stack, newest rule first.

    Rules already gone (404, or their id now holds another rule) are dropped, never
    retried; on a backend failure the rest stay so "undo" can try again.
    """
    if not undo_stack:
        return NOTHING_TO_UNDO
    entry = undo_stack[-1]
    rules, what = entry['rules'], entry['what']
    total, removed, gone, applied = len(rules), 0, 0, False
    try:
        current = {row.get('id'): row for row in request('/rules')}
    except Exception:
        return f"I can't reach Eddie's backend, so I couldn't undo the {what}. Nothing changed."
    while rules:
        rule = rules[-1]
        if not still_voice_rule(current.get(rule['id']), rule):
            rules.pop()
            gone += 1
            continue
        try:
            result = request(f"/rules/{rule['id']}", method='DELETE', timeout=RULE_TIMEOUT)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code != 404:
                break
            rules.pop()
            gone += 1
            continue
        except Exception:
            break
        if result.get('deleted') is not True or result.get('id') != rule['id']:
            break
        rules.pop()
        removed += 1
        applied = result.get('applied') is True  # DNS loads whole revisions; the last delete covers the rest
    if rules:
        if removed == 0:
            return f"I couldn't undo the {what}; the backend didn't confirm it. Nothing changed."
        return (f"I only removed {removed} of the {total} {what} before the backend stopped "
                "answering. Say undo again to remove the rest.")
    undo_stack.pop()
    if removed == 0:
        return f"The {what} had already been removed elsewhere, so there was nothing to undo."
    if not applied:
        return (f"I removed the {what}, but Eddie's DNS hasn't loaded the change yet, "
                "so it may still apply for a moment.")
    return f"Undone. I removed the {what}, and Eddie's DNS has loaded the change."


LEAD = r'(?:(?:ok|okay|eddie|hey eddie|please) )*(?:(?:can|could|would|will) you (?:please )?)?'
DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
NUMBERS = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
           'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
AWAY_MAX_DAYS = 30  # POST /mode refuses an end time further ahead
AWAY_ON = re.compile(
    LEAD + r'(?:(?:turn|switch) on away mode|(?:turn|switch) away mode on|enable away mode|start away mode|'
    r'(?:i am|im|we are|were) (?:leaving|going away|heading out|going out of town|out of town|away))'
    r'(?: (?P<when>for the weekend|this weekend|for (?P<n>a|an|one|two|three|four|five|six|seven|eight|nine|ten|'
    r'[1-9][0-9]{0,2}) (?P<unit>hours?|days?|nights?)|until (?P<day>tomorrow|' + '|'.join(DAYS) + r')))?(?: please)?')
AWAY_OFF = re.compile(
    LEAD + r'(?:(?:turn|switch) off away mode|(?:turn|switch) away mode off|disable away mode|stop away mode|'
    r'end away mode|(?:i am|im|we are|were) (?:back|home|back home)(?: now)?)(?: please)?')
AWAY_TOO_LONG = f"I can only set away mode for up to {AWAY_MAX_DAYS} days. Nothing changed."


def away_end(match, now=None):
    """(end time in ms or None, spoken " until ...") for AWAY_ON's optional time phrase.

    A day means the end of that day; "the weekend" the end of the coming Sunday.
    """
    if not match['when']:
        return None, ''
    now = now or datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if match['n']:
        n = NUMBERS.get(match['n']) or int(match['n'])
        end = now + (timedelta(hours=n) if match['unit'].startswith('hour') else timedelta(days=n))
        return int(end.timestamp() * 1000), f" until {end:%A %-I:%M %p}"
    if match['day'] == 'tomorrow':
        days, name = 1, 'tomorrow'
    elif match['day']:
        days, name = (DAYS.index(match['day']) - now.weekday()) % 7 or 7, match['day'].capitalize()
    else:
        days, name = (6 - now.weekday()) % 7, 'Sunday'
    return int((midnight + timedelta(days=days + 1)).timestamp() * 1000), f' until the end of {name}'


def away_command(text, now=None):
    """RISKY_COMMANDS parser: Confirmation.request args to turn away mode on or off,
    a spoken refusal for an end time too far ahead, else None."""
    if AWAY_OFF.fullmatch(text):
        return 'away', {'on': False}, 'Turn off away mode?'
    match = AWAY_ON.fullmatch(text)
    if not match:
        return None
    now = now or datetime.now()
    until, when = away_end(match, now)
    if until is not None and until > (now + timedelta(days=AWAY_MAX_DAYS)).timestamp() * 1000:
        return AWAY_TOO_LONG
    prompt = (f"Turn on away mode{when}? I'll cut telemetry from the devices whose traffic shows when "
              "someone's home. Cameras, doorbells and alarms stay up.")
    return 'away', {'on': True, 'until': until, 'when': when}, prompt


# A display name that's only a MAC, IPv4 or IPv6 address (devices.display_name's last resort).
ADDRESS = re.compile(r'(?:ip:)?(?:[0-9a-f]{2}(?::[0-9a-f]{2}){5}|\d{1,3}(?:\.\d{1,3}){3}|[0-9a-f]*:[0-9a-f:]*)')


def some(devices, most=3):
    """Device names to speak: labels and vendor names, unlabelled addresses only counted."""
    names = [d['name'] for d in devices if not ADDRESS.fullmatch(str(d['name']).lower())]
    unnamed = len(devices) - len(names)
    if unnamed:
        names.append(f"{unnamed} unnamed device{'s' if unnamed > 1 else ''}" if names else
                     f"{unnamed} device{'s' if unnamed > 1 else ''} without a name")
    return join(names) if len(names) <= most else f"{', '.join(names[:most])} and {len(names) - most} more"


def set_away_mode(on, until=None, when=''):
    """RISKY_ACTIONS['away']: POST /mode and speak only what the backend acknowledged.

    On means DNS loaded it; never that a device's traffic was seen blocked.
    """
    body = {'away': on}
    if until is not None:
        body['away_until'] = until
    result = request('/mode', body, timeout=RULE_TIMEOUT)
    if result.get('away') is not on or result.get('away_until') != until:
        raise ValueError('Away mode was not acknowledged')
    applied = result.get('away_applied') is True
    if not on:
        if not applied:
            return "I turned away mode off, but Eddie's DNS hasn't loaded the change yet, so it may still block for a moment."
        return "Away mode is off, and Eddie's DNS has loaded the change."
    if not applied:
        return f"I saved away mode{when}, but Eddie's DNS hasn't loaded it yet, so nothing is blocked yet."
    covered = result.get('away_devices') or []
    exempt = result.get('away_exempt') or []
    if not covered:
        return (f"Away mode is on{when}, but I haven't seen a device whose traffic shows someone's home this week, "
                "so it isn't blocking anything.")
    kept = f" {some(exempt)} {'stays' if len(exempt) == 1 else 'stay'} up." if exempt else ''
    return f"Away mode is on{when}. Eddie's DNS has loaded it and is cutting telemetry from {some(covered)}.{kept}"


def forget_conversation():
    """Drop the remembered domain when the conversation or microphone session ends."""
    context['domain'] = None


CONFIRM_SECONDS = 15
YES = {'yes', 'yeah', 'yep', 'yes please', 'do it', 'yes do it', 'go ahead', 'confirm'}
NO = {'no', 'nope', 'no thanks', 'cancel', 'cancel it', 'stop', 'dont', 'never mind'}
# Risky actions run only from Confirmation.reply after an explicit yes. Each maps
# kind -> callable(**args) returning the spoken result only after backend
# acknowledgment. RISKY_COMMANDS parse normalized text -> (kind, args, prompt).
# Broad allows (a service or a wildcard), lockdown and new devices are parsed in route,
# not RISKY_COMMANDS: allows need the raw text's dots, the others a /devices lookup.
# block and home_mode are only requested by the local model's write tools
# (ollama_agent.py); spoken "block x.com" and "turn on home mode" run directly.
RISKY_ACTIONS = {'allow': allow_patterns, 'lockdown': lockdown_device, 'block': block_action,
                 'home_mode': home_mode_action, 'away': set_away_mode,
                 'approve': lambda **args: decide_new_device('approve', **args),
                 'deny': lambda **args: decide_new_device('deny', **args)}
RISKY_COMMANDS = [away_command]


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
# Optional local model for questions and plain-English rules: callable(raw text) ->
# reply, or None when the model is unavailable. Its changes only reach
# confirmation.request, never the backend. Set by frontend/serve.py (ollama_agent).
ask_model = None
# Optional local model that writes "what they know" inferences from the backend's
# facts: callable(facts) -> text, or None to keep the backend's template.
phrase_inferences = None
TIME_NUMBERS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
                'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'fourteen': 14,
                'thirty': 30}


def report_hours(text):
    """Requested lookback in whole hours, or None to ask the backend for all retained data."""
    lower = text.lower()
    match = re.search(r'\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fourteen|thirty)\s*'
                      r'(minutes?|hours?|days?|weeks?|months?)\b', lower)
    if match:
        number = int(match[1]) if match[1].isdigit() else TIME_NUMBERS[match[1]]
        unit = match[2]
        if unit.startswith('minute'):
            return max(1, (number + 59) // 60)
        return number * {'hour': 1, 'day': 24, 'week': 168, 'month': 720}[unit.rstrip('s')]
    if re.search(r'\b(?:today|yesterday|last day|past day)\b', lower):
        return 24
    if re.search(r'\b(?:this|last|past|previous) week\b', lower):
        return 168
    if re.search(r'\b(?:this|last|past|previous) month\b', lower):
        return 720
    return None


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
    new_device = NEW_DEVICE.fullmatch(words(raw))
    if new_device:
        command = new_device_request(new_device['verb'])
        return command if isinstance(command, str) else confirmation.request(*command)
    allow = allow_target(raw)
    if allow and allow[0] in ('service', 'wildcard'):
        return confirmation.request(*allow_request(*allow))
    lockdown = lockdown_target(raw)
    if lockdown and lockdown[1]:
        command = lockdown_request(*lockdown)
        return command if isinstance(command, str) else confirmation.request(*command)
    for parse in RISKY_COMMANDS:
        command = parse(text)
        if command:
            return command if isinstance(command, str) else confirmation.request(*command)
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
    if block and block != ('domain', None):
        return block_command(*block)
    if allow and allow[0] != 'unknown':
        return allow_command(*allow)
    if UNDO.fullmatch(text):
        return undo_command()
    # A change the commands above can't parse ("lock down the TV to Netflix") goes to the
    # local model, whose write tools only reach confirmation; without it, nothing changes.
    if block or allow or re.search(r'\b(block|allow|lock|undo|email|send|mode)\b', text):
        reply = ask_model(raw) if ask_model is not None else None
        if reply is not None:
            return reply
        if block:
            return NOT_RESOLVED
        if allow:
            return ALLOW_UNRESOLVED
        return "I haven't changed anything. I can block or allow a domain, allow a service like Netflix, lock down a device, allow or deny a new device, undo my last rule, or turn home or away mode on or off; email isn't connected yet."
    if re.search(r'\bwhat (?:do|does|can|could) (?:\w+|my \w+|the \w+) (?:know|tell|guess|figure out|learn) about (?:me|us|my house|my home|our house)\b', text):
        try:
            report = what_they_know(report_hours(raw))
        except Exception:
            return "I can't reach Eddie’s traffic backend, so I can't say what they know right now."
        if report.get('enough_data') is False:
            return str(report['text'])
        if phrase_inferences is not None and report.get('facts'):
            reply = phrase_inferences(report['facts'])
            if reply:
                return " ".join(filter(None, (report.get('notice'), reply)))
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
