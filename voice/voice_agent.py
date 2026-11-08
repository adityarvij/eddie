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

import guest
import house_guide
import texting
import timers


def request(path, body=None, timeout=3, method=None):
    base = os.environ.get('EDDIE_API_URL', 'http://127.0.0.1:8000').rstrip('/')
    data = None if body is None else json.dumps(body).encode()
    headers = {'Content-Type': 'application/json'}
    if os.environ.get('EDDIE_API_TOKEN'):  # needed when the API isn't on this machine's loopback (#96)
        headers['Authorization'] = 'Bearer ' + os.environ['EDDIE_API_TOKEN']
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


# Guest Mode (#163). In memory here; frontend/serve.py swaps in the file-backed profile.
# Every tool below checks profile.check(category) before it runs.
profile = guest.Profile()


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
    return {'home_mode': enabled, 'home_applied': result.get('home_applied') is True}


def home_mode_reply(enabled):
    """Set home mode and say so; it's enforced only once /mode reports DNS loaded it (#20)."""
    try:
        result = set_home_mode(enabled)
    except Exception:
        return "I couldn't change home mode."
    return mode_turned('Home mode', enabled, result['home_applied'])


def mode_turned(name, on, applied):
    """Every mode's spoken result: "<name> turned on/off." once DNS loaded it, else only saved."""
    state = 'on' if on else 'off'
    return f"{name} turned {state}." if applied else f"{name} saved as {state}, but Eddie's DNS hasn't loaded it yet."


# Home mode by voice. The whole request must be the command, so negations, questions and extra
# actions can't flip it, but up to two words may lead in: on the Pi, Whisper heard "Eddie, turn on
# home mode please" as "Itty, turn on home mode please", and people say "home mode on" with no verb.
_HOME = r'(?:the )?home ?mode'
_HOME_ON = (rf'(?:(?:turn|switch|flip|put) on {_HOME}|(?:turn|switch|flip|put|set) {_HOME} on|(?:enable|activate|start) {_HOME}'
            rf'|(?:i want|id like) {_HOME} (?:on|enabled)|{_HOME} on)')
_HOME_OFF = (rf'(?:(?:turn|switch|flip|put) off {_HOME}|(?:turn|switch|flip|put|set) {_HOME} off|(?:disable|deactivate|stop|end) {_HOME}'
             rf'|(?:i want|id like) {_HOME} (?:off|disabled)|{_HOME} off)')
_MODE_TAIL = r'(?: please| now| for me| eddie)*'
NOT_A_LEAD_IN = {'dont', 'do', 'not', 'never', 'no', 'is', 'was', 'are', 'isnt', 'what', 'whats', 'why', 'how', 'did',
                 'does', 'should', 'if', 'when', 'might', 'maybe', 'whether', 'and', 'or', 'but'}


def home_mode_command(text):
    """True or False for a whole-utterance home mode on/off command, else None."""
    for on, core in ((True, _HOME_ON), (False, _HOME_OFF)):
        match = re.fullmatch(rf'(?:(?P<lead>[a-z0-9]+(?: [a-z0-9]+)?) )?{LEAD}(?:go ahead and )?{core}{_MODE_TAIL}', text)
        if match and not set((match['lead'] or '').split()) & NOT_A_LEAD_IN:
            return on
    return None


# Guest Mode by voice (#163), phrased like home mode. Only the owner reaches it (a guest gets the
# modes refusal first), and only to turn it on: Eddie can't tell voices apart, so "turn off guest
# mode" would let any guest out. Turning it off stays on the dashboard's guest flag.
_GUEST = r'(?:the )?guest ?mode'
_GUEST_ON, _GUEST_OFF = _HOME_ON.replace(_HOME, _GUEST), _HOME_OFF.replace(_HOME, _GUEST)


def guest_mode_command(text):
    """True or False for a whole-utterance guest mode on/off command, else None."""
    for on, core in ((True, _GUEST_ON), (False, _GUEST_OFF)):
        match = re.fullmatch(rf'(?:(?P<lead>[a-z0-9]+(?: [a-z0-9]+)?) )?{LEAD}(?:go ahead and )?{core}{_MODE_TAIL}', text)
        if match and not set((match['lead'] or '').split()) & NOT_A_LEAD_IN:
            return on
    return None


def guest_mode_reply(on):
    """The owner's spoken Guest Mode switch: on at once, like the dashboard's; off only from the dashboard."""
    if not on:
        return "Guest mode is already off."
    try:
        profile.update(active=True)
    except OSError:
        return "I couldn't turn on guest mode. Nothing changed for guests."
    # As on the dashboard: a pending yes and the remembered domain don't carry over to the guest.
    confirmation.cancel()
    forget_conversation()
    return f"Guest mode turned on. {guest_help()} Turn it off with the guest flag on the dashboard."


def home_mode_action(enabled):
    """RISKY_ACTIONS['home_mode']: the model's set_mode after a yes."""
    return home_mode_reply(enabled)


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


BURGLARS_EYE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'burglars_eye',
        'description': ('When could someone tell the house was empty, from recent traffic, with devices ranked '
                        'by occupancy leak score. Security devices are flagged never_block.'),
        'parameters': {
            'type': 'object',
            'properties': {'hours': {'type': 'integer', 'minimum': 1, 'maximum': 8760,
                                     'description': 'Requested lookback in hours. Omit for a week.'}},
            'additionalProperties': False,
        },
    },
}


def burglars_eye(hours=None):
    """Backend-computed occupancy report only; the reply text comes from /burglars-eye."""
    if hours is None:
        return request('/burglars-eye')
    if type(hours) is not int or not 1 <= hours <= 8760:
        raise ValueError('hours must be an integer from 1 to 8760')
    return request(f'/burglars-eye?hours={hours}')


WHAT_THEY_KNOW = re.compile(
    r'\bwhat (?:do|does|can|could) (?:\w+|my \w+|the \w+) (?:know|tell|guess|figure out|learn) about (?:me|us|my house|my home|our house)\b')
EMPTY_HOUSE = re.compile(
    r'\b(?:burglars?|(?:house|home|place)(?: (?:was|is|looks?|looked|looking|seems?|seemed|been|be|sits?|sat))* empty'
    r'|(?:nobody|no one|noone)(?:s| is| was)? (?:home|in|around))\b')


# A domain as POST /rules accepts it (no wildcard), and a domain inside a spoken reply.
# Privacy settings coach (#112): guidance only; the reply text comes from /suggestions.
PRIVACY_TIPS = re.compile(
    r'\b(?:(?:how|what) (?:can|could|should|do) (?:i|we) (?:do to )?(?:improve|better|protect|boost|increase) '
    r'(?:my|our) privacy|(?:make|keep) (?:my|our|the) (?:\w+ ){0,2}more private|privacy (?:tips|suggestions|advice)'
    r'|what (?:privacy )?settings should (?:i|we) change|(?:any|some|your|give me) suggestions)\b')


def privacy_suggestions():
    return request('/suggestions')


DOMAIN = re.compile(r'[a-z0-9_-]+(?:\.[a-z0-9_-]+)+')
SPOKEN_DOMAIN = re.compile(r'\b[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.[a-z]{2,}\b')
# eddie/labels.py TRACKING; copied so the voice worker doesn't import the backend.
TRACKING = {'telemetry', 'ads', 'analytics', 'tracking', 'id-sync'}
IT_WINDOW_MS = 15 * 60 * 1000  # "it" never reaches back further than this in /events
RULE_TIMEOUT = 5  # POST /rules waits up to 2 s for the DNS engine before answering
IT_WORDS = r'it|that|this|that one|this one|that domain|this domain|that tracker|this tracker'
WILDCARD = re.compile(r'\*\.[a-z0-9_-]+(?:\.[a-z0-9_-]+)+')


def _command(verb):
    return re.compile(
        r'(?:(?:ok|okay|eddie|please) )*(?:(?:can|could|would|will) you (?:please )?)?(?:go ahead and )?'
        rf'{verb} (?P<target>.+?)(?: (?:please|for me|now|too|as well))*')


NOT_CAUGHT = "I didn't quite catch that."
# Rather than reading every capability aloud, Eddie points at the dashboard's capabilities pane.
CAPABILITIES_HINT = "Tap capabilities to see what I can do."
SAVED_NOT_LOADED = "Saved, but Eddie's DNS hasn't loaded it yet."
BLOCK = _command('block')
ALLOW = _command('allow')
NOT_RESOLVED = 'Which domain? Say block, then the full name.'
RULE_FAILED = "I couldn't add that rule."
# Spoken service name -> patterns for "allow <service>"; one file, covered by tests/test_voice_allow.py.
SERVICES = {key: value for key, value in json.loads(
    (Path(__file__).with_name('services.json')).read_text()).items() if not key.startswith('_')}
SERVICE_ALIASES = {alias: key for key, service in SERVICES.items() for alias in service['aliases']}
ALLOW_UNRESOLVED = "Which service or domain should I allow?"
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


def add_rule(pattern, action, saved=None, device=None, by='voice'):
    """POST a rule (global, or for one device key); returns applied, raising unless
    the backend stored this rule. by is its created_by: voice, or dashboard for kid
    mode switched from the dashboard (#119).

    The acknowledged rule is appended to saved, for remember_rules.
    """
    body = {'pattern': pattern, 'action': action, 'created_by': by}
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
    return f"{domain} is essential, so I won't block it."


def loaded(said, applied):
    """A change's spoken result: said once Eddie's DNS has loaded it, else only that it's saved."""
    return f"{said}." if applied else SAVED_NOT_LOADED


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
            return UNREACHABLE
    if domain is None:
        return NOT_RESOLVED
    if essential(domain):
        return essential_reply(domain)
    try:
        applied = block_domain(domain)
    except Exception:
        return RULE_FAILED
    return loaded(f'Blocked {domain}', applied)


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
        prompt = f'Allow {name}?'
    else:
        name, patterns = f'{value[2:]} and every subdomain', [value]
        prompt = f'Allow {name}?'
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
        return f"I only allowed part of {name}; it may still be blocked."
    return loaded(f'Allowed {name}', applied)


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
    return loaded(f'Allowed {value}', applied)


LOCKDOWN = _command(r'lock ?down')
LOCKDOWN_ARTICLE = re.compile(r'(?:the|my|our) ')
# Services a locked-down device keeps by default (#17): every streaming service
# voice knows. The allowlist's essentials (sign-in, updates, time) survive anyway.
LOCKDOWN_KEEP = [key for key, service in SERVICES.items() if not service.get('kids')]
# What a child's device keeps (#119): in kid mode, and when "lock down <device>" names a
# device marked as a child's on the dashboard. voice/kid_safe.json is the list to edit.
KID_KEEP = json.loads(Path(__file__).with_name('kid_safe.json').read_text())['keep']
if not KID_KEEP or set(KID_KEEP) - SERVICES.keys():
    raise ValueError('voice/kid_safe.json keep must list services.json keys')
UNREACHABLE = "I can't reach Eddie's backend."
LOCKDOWN_UNREACHABLE = UNREACHABLE


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


def named(device):
    return bool(device.get('label') and words(device['label']))


# "List my rules" / "list my devices": Eddie reads them out, the same lists the dashboard's footer opens.
_LIST = (r'(?:(?:hey|ok|okay) )?(?:eddie )?(?:please )?(?:(?:can|could|would|will) you )?'
         r'(?:list|read|read out|show|show me|tell me|go through|give me|what are)(?: me)?(?: all)?(?: of)?'
         r'(?: (?:my|the|our|your|all))? {}(?: please| for me)?')
LIST_RULES = re.compile(_LIST.format('rules') + r'|what rules (?:do i|do we|are there|have i|have we)(?: have| set| made)?')
LIST_DEVICES = re.compile(_LIST.format('devices') + r'|what devices (?:do i have|do we have|are there|are on (?:the|my) (?:network|wifi)|are connected|have you seen)')
LIST_MAX = 5  # read this many aloud; the rest are on the dashboard


def spoken_device(d):
    """A name worth saying: the label, else the maker, never the MAC the dashboard falls back to."""
    if named(d):
        return d['label']
    return f"a {d['vendor']} device" if d.get('vendor') else 'an unnamed device'


def list_rules():
    try:
        rules = request('/rules')
    except Exception:
        return UNREACHABLE
    if not rules:
        return "You don't have any rules yet."
    def phrase(rule):
        target = 'everything' if rule['pattern'] == '*' else rule['pattern']
        where = f" on {rule['device']}" if rule.get('device') else ''
        return f"{rule['action']} {target}{where}"
    # Two rules can say the same thing (one from the dashboard, one by voice): read each once.
    distinct = list(dict.fromkeys(phrase(rule) for rule in rules))
    said = distinct[:LIST_MAX]
    if len(distinct) > LIST_MAX:
        said.append(f"{len(distinct) - LIST_MAX} more on the dashboard")
    return f"You have {len(rules)} rule{'' if len(rules) == 1 else 's'}: {join(said)}."


def list_devices():
    try:
        devices = request('/devices')['devices']
    except Exception:
        return UNREACHABLE
    if not devices:
        return "I haven't seen any devices in the last day."
    busiest = sorted(devices, key=lambda d: d.get('queries', 0), reverse=True)
    said = [f"{spoken_device(d)} made {d.get('queries', 0)} lookups, {d.get('blocked', 0)} blocked"
            for d in busiest[:LIST_MAX]]
    if len(devices) > LIST_MAX:
        said.append(f"{len(devices) - LIST_MAX} more on the dashboard")
    listed = '; '.join(said)
    return f"I've seen {len(devices)} device{'' if len(devices) == 1 else 's'} today. {listed[:1].upper()}{listed[1:]}."


def keep_text(keep):
    names = [SERVICES[key]['name'] for key in keep]
    return join(names) if names else "the essentials"


def lockdown_request(article, spoken, keep=None):
    """Confirmation.request args to lock a device down to the keep services (SERVICES
    keys; by default KID_KEEP for a device marked as a child's, else LOCKDOWN_KEEP), or
    the spoken reply when the device is unknown, ambiguous or already locked down
    (nothing changes then). Only named devices can be found, so "lock down ---" can't."""
    try:
        devices = request('/devices')['devices']
    except Exception:
        return LOCKDOWN_UNREACHABLE
    found = find_devices(spoken, devices)
    if not found:
        return f"I don't know a device called {spoken}."
    if len({d['mac'] for d in found}) > 1:
        return f"Which one: {join([d['label'] for d in found], 'or')}?"
    device = found[0]
    name = article + device['label']
    try:
        locked = any(r.get('pattern') == '*' and r.get('device_mac') == device['mac'] for r in request('/rules'))
    except Exception:
        return LOCKDOWN_UNREACHABLE
    if locked:
        return f'{name[0].upper()}{name[1:]} is already locked down.'
    child = keep is None and device.get('kid')
    if keep is None:
        keep = KID_KEEP if child else LOCKDOWN_KEEP
    prompt = f"Lock down {name} to {keep_text(keep)}?"
    return 'lockdown', {'device': device['mac'], 'name': name, 'keep': list(keep)}, prompt


def block_action(domain):
    """RISKY_ACTIONS['block']: the model's block tool after a yes."""
    return block_command('domain', domain)


def join(names, last='and'):
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} {last} {names[-1]}"


GUEST_TOPICS_SPOKEN = 6


def guest_help(every=False):
    """What a guest may ask, from the owner's approvals and the guide's guest topics. Names topics, never answers."""
    names = house_guide.topics('guest') if profile.allows('house_guide') else []
    shown = names[:GUEST_TOPICS_SPOKEN if every else 3]
    asks = [f"ask me about {join(shown)}"] if shown else []
    if profile.allows('timers'):
        asks.append('set a timer')
    if not asks:
        return "Your host hasn't turned anything on for guests yet."
    reply = f"You can {', or '.join(asks)}." if len(asks) > 1 else f"You can {asks[0]}."
    return f"{reply} Tap capabilities for the rest." if len(names) > len(shown) else reply


def delete_rules(rules):
    """Delete acknowledged rules newest first, popping each one the backend removed
    (or no longer has). Stops at the first failure; what's left is still stored.
    Returns whether DNS loaded the last delete (it loads whole revisions)."""
    applied = False
    while rules:
        try:
            result = request(f"/rules/{rules[-1]['id']}", method='DELETE', timeout=RULE_TIMEOUT)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code != 404:
                return applied
        except Exception:
            return applied
        else:
            if result.get('deleted') is not True or result.get('id') != rules[-1]['id']:
                return applied
            applied = result.get('applied') is True
        rules.pop()
    return applied


def lock_device(device, name, keep, by='voice'):
    """Device allow rules for every keep pattern, then the device's "*" block rule last,
    so a device is never blocked without its keep list. Returns (status, spoken text),
    status applied, saved (DNS hasn't loaded it) or failed.

    On any failure the saved rules are deleted again; ones that can't be stay on
    undo_stack (voice only: by='dashboard' rules aren't voice's to undo). Success
    means the rules are saved and loaded, never that traffic was seen blocked.
    """
    patterns = [pattern for key in keep for pattern in SERVICES[key]['patterns']]
    remember = remember_rules if by == 'voice' else lambda what, saved: None
    rules, applied = [], False
    try:
        for pattern in patterns:
            add_rule(pattern, 'allow', rules, device, by)
        applied = add_rule('*', 'block', rules, device, by)
    except Exception:
        saved = len(rules)
        delete_rules(rules)
        remember(f'leftover lockdown rules for {name}', rules)
        if saved == 0:
            return 'failed', f"I couldn't lock down {name}."
        if not rules:
            return 'failed', f"I couldn't lock down {name}."
        fix = 'say undo to remove them.' if by == 'voice' else 'turning kid mode off removes them.'
        return 'failed', f"I couldn't lock down {name}, and some of its rules are left; {fix}"
    remember(f'lockdown rules for {name}', rules)
    said = loaded(f"Locked down {name}", applied)
    return ('applied' if applied else 'saved'), said


def lockdown_device(device, name, keep):
    """RISKY_ACTIONS['lockdown']: lock_device's spoken result."""
    return lock_device(device, name, keep)[1]


def unlock_device(device, name, keep, by='voice'):
    """Remove a device's lockdown: its "*" block rule first, so it's never left blocked
    without its keep list, then its allow rules for the keep patterns. Rules for the
    device that kid mode didn't make stay. Returns (status, spoken text) like lock_device."""
    patterns = {pattern for key in keep for pattern in SERVICES[key]['patterns']}
    try:
        mine = [row for row in request('/rules') if row.get('device_mac') == device]
    except Exception:
        return 'failed', f"I can't reach Eddie's backend, so {name} is still locked down."
    keeps = [row for row in mine if row.get('action') == 'allow' and row.get('pattern') in patterns]
    blocks = [row for row in mine if row.get('action') == 'block' and row.get('pattern') == '*']
    rules = keeps + blocks  # delete_rules works from the end: the "*" blocks go first
    applied = delete_rules(rules)
    if any(row in rules for row in blocks):
        return 'failed', f"I couldn't unlock {name}."
    return ('applied' if applied else 'saved'), loaded(f"Unlocked {name}", applied)


# Kid mode (#119): every device marked as a child's on the dashboard, locked down to KID_KEEP
# ("turn on kid mode", "lock down the kids' devices") or unlocked ("turn off kid mode").
KID_MODE = re.compile(
    r'(?:(?:ok|okay|eddie|please) )*(?:(?:can|could|would|will) you (?:please )?)?(?:go ahead and )?'
    r'(?:(?:turn|switch) (?P<on>on|off) (?:the )?kids? mode|(?:turn|switch) (?:the )?kids? mode (?P<on2>on|off)'
    r'|(?P<verb>enable|disable|start|stop) (?:the )?kids? mode|kids? mode (?P<on3>on|off)'
    r'|(?P<lock>lock ?down|unlock) (?:the |my |our |all (?:of )?(?:the |my |our )?)?(?:kids?|childrens?|childs) devices)'
    r'(?: (?:please|for me|now))*')
KID_UNREACHABLE = UNREACHABLE


def kid_mode_target(raw):
    """True to turn kid mode on, False for off, None if this isn't a kid mode command."""
    match = KID_MODE.fullmatch(words(raw))
    if not match:
        return None
    if switch := match['on'] or match['on2'] or match['on3']:
        return switch == 'on'
    if match['verb']:
        return match['verb'] in ('enable', 'start')
    return match['lock'] != 'unlock'


def kid_name(device):
    if named(device):
        return device['label']
    return f"an unnamed {device['vendor']} device" if device.get('vendor') else 'an unnamed device'


def kid_mode_request(on):
    """set_kid_mode's args (as Confirmation.request args) to turn kid mode on or off for every
    child's device not already that way, or the spoken reply when there's nothing to change."""
    try:
        devices = request('/devices')['devices']
        locked = {row.get('device_mac') for row in request('/rules')
                  if row.get('pattern') == '*' and row.get('action') == 'block'}
    except Exception:
        return KID_UNREACHABLE
    kids = [d for d in devices if d.get('kid')]
    if not kids:
        return "No device is marked as a child's yet; set one on the dashboard."
    todo = [d for d in kids if (d['mac'] in locked) != on]
    if not todo:
        return f"Kid mode is already {'on' if on else 'off'}."
    targets = [{'device': d['mac'], 'name': kid_name(d)} for d in todo]
    return 'kid_mode', {'on': on, 'devices': targets}, f"Turn {'on' if on else 'off'} kid mode?"


def set_kid_mode(on, devices, by='voice'):
    """RISKY_ACTIONS['kid_mode']: lock each child's device down to KID_KEEP, or unlock it.
    Speaks only what the backend acknowledged, never that traffic was seen blocked."""
    change = lock_device if on else unlock_device
    results = [(d['name'], *change(d['device'], d['name'], KID_KEEP, by)) for d in devices]
    failed = [text for _, status, text in results if status == 'failed']
    if failed:
        partly = f"Kid mode only partly turned {'on' if on else 'off'}." if len(failed) < len(results) else ''
        return ' '.join(filter(None, [partly, *failed]))
    return mode_turned('Kid mode', on, all(status == 'applied' for _, status, _ in results))


def kid_mode_change(on, by='dashboard'):
    """Kid mode from the dashboard's button or a spoken command, both run at once: the spoken-style result."""
    command = kid_mode_request(on)
    if isinstance(command, str):
        return command
    return set_kid_mode(**command[1], by=by)


# "allow the new device" / "deny the new device" (#98): the newest device /devices
# lists as pending, read back with a yes before POST /devices/<mac>/approve|deny.
NEW_DEVICE = re.compile(
    r'(?:(?:ok|okay|eddie|please) )*(?:(?:can|could|would|will) you (?:please )?)?(?:go ahead and )?'
    r'(?P<verb>allow|approve|let in|deny|block|reject) (?:the |that |this )?new device(?: (?:please|for me|now))*')
NEW_DEVICE_STATUS = {'allow': 'approve', 'approve': 'approve', 'let in': 'approve',
                     'deny': 'deny', 'block': 'deny', 'reject': 'deny'}


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
        return "There's no new device waiting."
    device = pending[0]
    prompt = f"{'Allow' if decision == 'approve' else 'Deny'} the new device, {device['name']}?"
    return decision, {'device': device['mac'], 'name': device['name']}, prompt


def decide_new_device(decision, device, name):
    """RISKY_ACTIONS['approve'] and ['deny']: success only once the backend stored the decision."""
    want = 'approved' if decision == 'approve' else 'denied'
    result = request(f'/devices/{device}/{decision}', {}, timeout=RULE_TIMEOUT)
    if result.get('mac') != device or result.get('status') != want:
        return f"I couldn't {decision} {name}."
    return loaded(f"{name} {want}", result.get('applied') is True)


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
    removed, gone, applied = 0, 0, False
    try:
        current = {row.get('id'): row for row in request('/rules')}
    except Exception:
        return UNREACHABLE
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
            return "I couldn't undo that."
        return "I only undid part of that; say undo again."
    undo_stack.pop()
    if removed == 0:
        return "That was already removed."
    if not applied:
        return f"Removed the {what}, but Eddie's DNS hasn't loaded the change yet."
    return f"Undid the {what}."


# Ask before blocking (#79): ids of API questions already read out, so each is spoken once per worker run.
announced = set()
QUESTION_TIMEOUT = 1  # on wake, before listening: never keep the user waiting on the backend


def question_announcement():
    """On wake: the newest unannounced API question as a pending confirmation, else None.

    A spoken yes runs answer_question; no, silence or an expiry leave it open on the dashboard.
    """
    try:
        questions = request('/questions', timeout=QUESTION_TIMEOUT)
    except Exception:
        return None
    for question in reversed(questions if isinstance(questions, list) else []):
        qid, prompt = question.get('id'), question.get('prompt')
        if type(qid) is not int or not isinstance(prompt, str) or qid in announced:
            continue
        announced.add(qid)
        return confirmation.request('question', {'question_id': qid}, prompt)
    return None


def answer_question(question_id):
    """RISKY_ACTIONS['question']: answer Block through the API; says blocked only on applied: true."""
    try:
        result = request(f'/questions/{question_id}', {'answer': 'block', 'by': 'voice'}, timeout=RULE_TIMEOUT)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 404:
            return 'That was already answered.'
        return RULE_FAILED
    except Exception:
        return RULE_FAILED
    saved = [{'id': r['id'], 'pattern': r['pattern'], 'action': r['action'], 'ts': r.get('ts')}
             for r in result.get('rules') or [] if isinstance(r, dict) and type(r.get('id')) is int
             and r.get('action') == 'block' and r.get('created_by') == 'voice' and isinstance(r.get('pattern'), str)]
    if not saved:
        return RULE_FAILED
    names = join([r['pattern'] for r in saved])
    remember_rules(f"block on {names}" if len(saved) == 1 else f"blocks on {names}", saved)
    return loaded(f'Blocked {names}', result.get('applied') is True)


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
AWAY_TOO_LONG = f"Away mode can only last {AWAY_MAX_DAYS} days."


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
    return 'away', {'on': True, 'until': until, 'when': when}, f'Turn on away mode{when}?'


def set_away_mode(on, until=None, when=''):
    """RISKY_ACTIONS['away']: POST /mode and speak only what the backend acknowledged.

    Without the caveat it means DNS loaded it; never that a device's traffic was seen blocked.
    """
    body = {'away': on}
    if until is not None:
        body['away_until'] = until
    result = request('/mode', body, timeout=RULE_TIMEOUT)
    if result.get('away') is not on or result.get('away_until') != until:
        raise ValueError('Away mode was not acknowledged')
    return mode_turned('Away mode', on, result.get('away_applied') is True)


# Sensitivity modes (#110): spoken category word -> the mode POST /sensitivity takes.
SENSITIVITY_WORDS = {
    'health': 'health', 'medical': 'health', 'pharmacy': 'health',
    'kid': 'kids', 'kids': 'kids', 'children': 'kids', 'childrens': 'kids',
    'money': 'money', 'finance': 'money', 'finances': 'money', 'financial': 'money', 'banking': 'money',
    'shopping': 'money',
    'belief': 'beliefs', 'beliefs': 'beliefs', 'religion': 'beliefs', 'religious': 'beliefs',
    'politics': 'beliefs', 'political': 'beliefs', 'news': 'beliefs',
}
_CATEGORY = r'(?P<cat>' + '|'.join(sorted(SENSITIVITY_WORDS, key=len, reverse=True)) + r')'
_THINGS = r'(?: (?:related|stuff|sites|apps|things|data|traffic))*'
SENSITIVITY_ON = [re.compile(LEAD + p + r'(?: please)?') for p in (
    r'protect (?:(?:anything|everything|all|my|our|the) )?(?:to do with )?' + _CATEGORY + _THINGS,
    r'(?:turn|switch) on ' + _CATEGORY + ' protection',
    r'(?:turn|switch) ' + _CATEGORY + ' protection on',
    r'enable ' + _CATEGORY + ' protection')]
SENSITIVITY_OFF = [re.compile(LEAD + p + r'(?: please)?') for p in (
    r'(?:stop|dont keep) protecting (?:(?:anything|everything|all|my|our|the) )?(?:to do with )?' + _CATEGORY + _THINGS,
    r'(?:turn|switch) off ' + _CATEGORY + ' protection',
    r'(?:turn|switch) ' + _CATEGORY + ' protection off',
    r'(?:disable|stop) ' + _CATEGORY + ' protection')]


def sensitivity_target(text):
    """(mode, on) for a whole-utterance sensitivity command in normalized text, else None."""
    for patterns, on in ((SENSITIVITY_OFF, False), (SENSITIVITY_ON, True)):
        for pattern in patterns:
            match = pattern.fullmatch(text)
            if match:
                return SENSITIVITY_WORDS[match['cat']], on
    return None


def sensitivity_reply(mode, on):
    """POST /sensitivity and read back only what the backend acknowledged.

    Without the caveat it means DNS loaded the mode; never that a tracker was seen blocked.
    """
    name = mode.capitalize()
    try:
        result = request('/sensitivity', {'mode': mode, 'on': on}, timeout=RULE_TIMEOUT)
        if result.get('mode') != mode or result.get('on') is not on:
            raise ValueError('Sensitivity mode was not acknowledged')
    except Exception:
        return f"I couldn't change {mode} protection."
    return mode_turned(f'{name} protection', on, result.get('applied') is True)


# Sends confirmed texts (texting.py); None when EDDIE_TEXTING is off or unsupported here.
text_sender = texting.configured_sender()
# Long enough to hear a word-for-word read-back of up to texting.MAX_BODY characters and answer.
TEXT_CONFIRM_SECONDS = 30
TEXT_REPLIES = {
    'sent': 'Sent to {name}.',
    'failed': "Messages couldn't send that to {name}.",
    'pending': "Messages hasn't confirmed it went to {name}; check before resending.",
    'unknown': "I can't confirm it went to {name}; check Messages before resending.",
}


def text_request(raw):
    """Confirmation.request args for "text <contact> <message>", a spoken refusal, or None.

    Never speaks the message or the address except in the read-back prompt.
    """
    if not texting.VERB.fullmatch(raw):
        return None
    if text_sender is None:
        return "Texting isn't set up."
    try:
        contacts = texting.load_contacts()
    except texting.ContactsError as error:
        return "I can't read your contacts."
    if not contacts:
        return "I don't have any contacts yet."
    contact, body = texting.parse(raw, contacts)
    if contact is None:
        return f"I can only text {join([c['name'] for c in contacts.values()], 'or')}."
    name = contact['name']
    if not body:
        return f"What should the text to {name} say?"
    if len(body) > texting.MAX_BODY:
        return "That's too long; keep it under 30 words."
    args = {'name': name, 'address': contact['address'], 'service': contact['service'], 'body': body}
    return 'text', args, f'Text {name}: "{body}" Send it?'


def send_text(name, address, service, body):
    """RISKY_ACTIONS['text']: says sent only once the sender reports it sent."""
    try:
        status = text_sender.send(address, service, body)
    except Exception:
        status = 'pending'
    return TEXT_REPLIES.get(status, TEXT_REPLIES['pending']).format(name=name)


def forget_conversation():
    """Drop the remembered domain when the conversation or microphone session ends."""
    context['domain'] = None


CONFIRM_SECONDS = 15
YES = {'yes', 'yeah', 'yep', 'yes please', 'do it', 'yes do it', 'go ahead', 'confirm'}
NO = {'no', 'nope', 'no thanks', 'cancel', 'cancel it', 'stop', 'dont', 'never mind'}
# Risky actions run only from Confirmation.reply after an explicit yes. Each maps
# kind -> callable(**args) returning the spoken result only after backend
# acknowledgment. RISKY_COMMANDS parse normalized text -> (kind, args, prompt).
# Broad allows (a service or a wildcard), lockdown, kid mode and new devices are parsed in route,
# not RISKY_COMMANDS: allows need the raw text's dots, the others a /devices lookup.
# block and home_mode are only requested by the local model's write tools
# (ollama_agent.py); spoken "block x.com" and "turn on home mode" run directly.
# question answers an API question read out on wake (#79). text is parsed in route
# (it needs the raw transcript word for word).
RISKY_ACTIONS = {'allow': allow_patterns, 'lockdown': lockdown_device, 'block': block_action,
                 'home_mode': home_mode_action, 'away': set_away_mode, 'question': answer_question,
                 'kid_mode': set_kid_mode,
                 'text': send_text,
                 'approve': lambda **args: decide_new_device('approve', **args),
                 'deny': lambda **args: decide_new_device('deny', **args)}
RISKY_COMMANDS = [away_command]
# Each risky action's permission category (#163); Confirmation checks it before asking and again before running.
RISKY_CATEGORIES = {'allow': 'rules', 'lockdown': 'rules', 'block': 'rules', 'question': 'rules',
                    'home_mode': 'modes', 'away': 'modes', 'kid_mode': 'modes', 'text': 'contacts',
                    'approve': 'devices', 'deny': 'devices'}


class Confirmation:
    """One pending risky action. Ambiguous replies and timeouts never execute."""
    def __init__(self, clock=time.monotonic, seconds=CONFIRM_SECONDS):
        self.clock = clock
        self.seconds = seconds
        self.pending = None
        self.lock = threading.Lock()

    def request(self, kind, args, prompt, seconds=None):
        if kind not in RISKY_ACTIONS:
            raise ValueError('Unknown risky action')
        denied = profile.check(RISKY_CATEGORIES[kind])
        if denied:
            return denied
        with self.lock:
            replaced = self.pending is not None
            self.pending = {'kind': kind, 'args': dict(args), 'prompt': prompt,
                            'expires_at': self.clock() + (seconds or self.seconds), 'reprompted': False}
        return f'{prompt} Yes or no?'

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
                    return "That expired; ask again."
                return None
            if text in NO:
                self.pending = None
                return 'Cancelled.'
            if text not in YES:
                if pending['reprompted']:
                    self.pending = None
                    return "No clear yes, so I didn't."
                pending['reprompted'] = True
                return f"Yes or no? {pending['prompt']}"
            self.pending = None
        # Guest Mode may have come on since the owner asked: a guest's yes never runs it.
        denied = profile.check(RISKY_CATEGORIES[pending['kind']])
        if denied:
            return denied
        try:
            return str(RISKY_ACTIONS[pending['kind']](**pending['args']))
        except Exception:
            return "I couldn't confirm that went through."


confirmation = Confirmation()
# Optional local model for questions and plain-English rules: callable(raw text) ->
# reply, or None when the model is unavailable. Its changes only reach
# confirmation.request, never the backend. Set by frontend/serve.py (ollama_agent).
ask_model = None
# Fast local semantic matcher for factual questions. Set by frontend/serve.py
# after its provisioned ONNX assets load; commands above always run first.
intent_model = None
# Optional local model that writes "what they know" inferences from the backend's
# facts: callable(facts) -> text, or None to keep the backend's template.
phrase_inferences = None
# Timers and reminders (#122). In memory here; frontend/serve.py swaps in the file-backed store and rings them.
timer_store = timers.Timers()
# A guest's own timers and reminders (#163), in memory only, so a guest never hears or cancels the household's.
guest_timers = timers.Timers()
# Turns the microphone off (#128): callable() -> True if it was on. Set by frontend/serve.py;
# None when there's no microphone to stop (tests, typed-only use).
microphone_off = None
STOP_MIC = re.compile(
    LEAD + r'(?:stop (?:recording|listening)|stop the (?:mic|microphone)|(?:turn|switch|shut) (?:off|down) the (?:mic|microphone)'
    r'|(?:turn|switch|shut) the (?:mic|microphone) (?:off|down)|(?:mic|microphone) off)(?: please| eddie)?')
STOP_MIC_TOOL = {
    'type': 'function',
    'function': {
        'name': 'stop_microphone',
        'description': 'Turn the microphone off when the user asks Eddie to stop recording or listening.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
}


def stop_microphone():
    """The router's and the local model's stop_microphone. Takes no yes: turning the mic off only shares less."""
    if microphone_off is None or not microphone_off():
        return 'The microphone is already off.'
    return 'Microphone turned off.'


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
    # Each tool checks its permission category first (#163): in Guest Mode, anything not
    # approved gets a fixed refusal before any lookup, so it never names a device or contact.
    if STOP_MIC.fullmatch(text):
        return profile.check('microphone_off') or stop_microphone()
    # A new risky command replaces any pending one; otherwise a pending action
    # consumes the reply, including missed speech.
    new_device = NEW_DEVICE.fullmatch(words(raw))
    if new_device:
        command = profile.check('devices') or new_device_request(new_device['verb'])
        return command if isinstance(command, str) else confirmation.request(*command)
    allow = allow_target(raw)
    if allow and allow[0] in ('service', 'wildcard'):
        return profile.check('rules') or confirmation.request(*allow_request(*allow))
    kid_mode = kid_mode_target(raw)
    if kid_mode is not None:
        # Runs at once, like home and guest mode: no yes/no.
        return profile.check('modes') or kid_mode_change(kid_mode, by='voice')
    guest_on = guest_mode_command(text)
    if guest_on is not None:
        return profile.check('modes') or guest_mode_reply(guest_on)
    lockdown = lockdown_target(raw)
    if lockdown and lockdown[1]:
        command = profile.check('rules') or lockdown_request(*lockdown)
        return command if isinstance(command, str) else confirmation.request(*command)
    if texting.VERB.fullmatch(raw):
        command = profile.check('contacts') or text_request(raw)
        return command if isinstance(command, str) else confirmation.request(*command, seconds=TEXT_CONFIRM_SECONDS)
    for parse in RISKY_COMMANDS:  # away mode only
        command = parse(text)
        if command:
            return (profile.check('modes') or command) if isinstance(command, str) else confirmation.request(*command)
    pending_reply = confirmation.reply(text)
    if pending_reply is not None:
        return pending_reply
    if not text:
        return NOT_CAUGHT
    # A guest gets a separate store, so the household's reminders are never read out or cancelled.
    # Without permission the command runs against a throwaway store, only to know it was one.
    store = timer_store if profile.role() == 'owner' else guest_timers
    reply = timers.handle(store if profile.allows('timers') else timers.Timers(), raw)
    if reply is not None:
        return profile.check('timers') or reply
    # Rules and devices are the household's traffic: a guest gets the same refusal as other traffic reads (#163).
    if LIST_RULES.fullmatch(text):
        return profile.check('traffic') or list_rules()
    if LIST_DEVICES.fullmatch(text):
        return profile.check('traffic') or list_devices()
    if not (WHAT_THEY_KNOW.search(text) or EMPTY_HOUSE.search(text) or PRIVACY_TIPS.search(text)):
        # The house guide (#165) speaks owner entries only to the owner (Guest Mode off). A guest
        # needs house_guide approved; a refused guest question is matched against guest entries only.
        reply = house_guide.answer(raw, profile.role())
        if reply is not None:
            return profile.check('house_guide') or reply
    on = home_mode_command(text)
    if on is not None:
        return profile.check('modes') or home_mode_reply(on)
    sensitive = sensitivity_target(text)
    if sensitive:
        return profile.check('rules') or sensitivity_reply(*sensitive)
    block = block_target(raw)
    if block and block != ('domain', None):
        return profile.check('rules') or block_command(*block)
    if allow and allow[0] != 'unknown':
        return profile.check('rules') or allow_command(*allow)
    if UNDO.fullmatch(text):
        return profile.check('rules') or undo_command()
    # A change the commands above can't parse ("lock down the TV to Netflix") goes to the
    # local model, whose write tools only reach confirmation; without it, nothing changes.
    if block or allow or re.search(r'\b(block|allow|lock|undo|email|send|mode)\b', text):
        denied = profile.check('rules')
        if denied:
            return denied
        reply = ask_model(raw) if ask_model is not None else None
        if reply is not None:
            return reply
        if block:
            return NOT_RESOLVED
        if allow:
            return ALLOW_UNRESOLVED
        return NOT_CAUGHT
    if WHAT_THEY_KNOW.search(text):
        denied = profile.check('reports')
        if denied:
            return denied
        try:
            report = what_they_know(report_hours(raw))
        except Exception:
            return UNREACHABLE
        if report.get('enough_data') is False:
            return str(report['text'])
        if phrase_inferences is not None and report.get('facts'):
            reply = phrase_inferences(report['facts'])
            if reply:
                return " ".join(filter(None, (report.get('notice'), reply)))
        return str(report['text'])
    if EMPTY_HOUSE.search(text):
        denied = profile.check('reports')
        if denied:
            return denied
        try:
            return str(burglars_eye(report_hours(raw))['text'])
        except Exception:
            return UNREACHABLE
    if PRIVACY_TIPS.search(text):
        denied = profile.check('devices')
        if denied:
            return denied
        try:
            return str(privacy_suggestions()['text'])
        except Exception:
            return UNREACHABLE
    if profile.role() == 'guest':
        # The local model's tools are all household data; guest concierge routing is #166.
        if text in ('hello', 'hi', 'hey', 'hey eddie'):
            return f"Welcome, I'm Eddie, your concierge. {guest_help()}"
        if house_guide.ASKS_TOPICS.fullmatch(house_guide.normal(raw)):
            return guest_help(every=True)
        if re.search(r'\b(summary|update|happening|going|requests|blocked|doing|activity)\b', text):
            return profile.check('traffic')
        return f"{guest.HELP} {guest_help()}"
    if text in ('hello', 'hi', 'hey', 'hey eddie'):
        return f"Hey, I'm Eddie. {CAPABILITIES_HINT}"
    if intent_model is not None:
        reply = intent_model(raw, context.get('domain'))
        if reply is not None:
            return reply
    if ask_model is not None:
        reply = ask_model(raw)
        if reply is not None:
            return reply
    if re.search(r'\b(summary|update|happening|going|requests|blocked|doing|activity)\b', text):
        try:
            return profile.check('traffic') or str(request('/summary')['text'])
        except Exception:
            return UNREACHABLE
    # A stray yes or no (nothing waiting for one) is just not caught; anything else gets the pointer.
    if text in ('yes', 'yeah', 'yep', 'sure', 'no', 'nope', 'ok', 'okay'):
        return NOT_CAUGHT
    return f"{NOT_CAUGHT} {CAPABILITIES_HINT}"
