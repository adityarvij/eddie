"""Voice timers and reminders (#122): parse the spoken command, keep them in a local file, ring when due.

Everything stays on this machine: a JSON file (owner-only) holds what's pending, and an
item is deleted from it the moment it rings or is cancelled.
"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import playback

MAX_ITEMS = 20
MAX_TIMER = 24 * 3600
MAX_REMINDER_DAYS = 7
LATE_SECONDS = 60  # rang this much after it was due (Eddie was off or busy): say so

LEAD = r'(?:(?:ok|okay|eddie|hey eddie|please) )*(?:(?:can|could|would|will) you (?:please )?)?'
SMALL = ('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen '
         'sixteen seventeen eighteen nineteen').split()
TENS = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90}
WORD_NUMBER = (r'(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?: (?:one|two|three|four|five|six|seven|eight|nine))?|'
               + '|'.join(sorted(SMALL[1:], key=len, reverse=True)) + ')')
NUMBER = rf'(?:\d{{1,3}}|{WORD_NUMBER}|an?)'
UNIT = r'(?:hours?|hrs?|minutes?|mins?|seconds?|secs?)'
PART = rf'(?:{NUMBER}) {UNIT}(?: and a half)?'
DURATION = rf'(?:half an hour|a half hour|{PART}(?: (?:and )?{PART})*)'
HOUR = r'(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)'
MINUTE = r'(?:\d\d|oh (?:one|two|three|four|five|six|seven|eight|nine)|(?:fifteen|thirty|forty five|' \
         r'(?:twenty|thirty|forty|fifty)(?: (?:one|two|three|four|five|six|seven|eight|nine))?|ten|eleven|twelve|' \
         r'thirteen|fourteen|sixteen|seventeen|eighteen|nineteen))'
CLOCK = (rf'(?:noon|midnight|(?P<h>{HOUR})(?::(?P<m>\d\d)| (?P<mw>{MINUTE}))?'
         r'(?: (?P<ap>am|pm|oclock|in the morning|in the afternoon|in the evening|at night|tonight))?)')
DAY = r'(?:today|tonight|tomorrow(?: morning| afternoon| evening| night)?|this (?:morning|afternoon|evening))'
WHEN = rf'(?:in (?P<dur>{DURATION})|(?:(?P<day1>{DAY}) )?(?:at|for) (?P<clock>{CLOCK})(?: (?P<day2>{DAY}))?|(?P<day3>{DAY}))'

TIMER_SET = re.compile(LEAD + rf'(?:(?:set|start|make|put on) )?(?:(?:a|an|the|my|me a) )?'
                       rf'(?:timer (?:for|of) (?P<dur>{DURATION})|(?P<dur2>{DURATION}) timer)(?: please)?')
REMIND_LEAD = LEAD + r'(?:remind me|(?:set|add|make|create) (?:a|me a) reminder)'
REMIND_AFTER = re.compile(REMIND_LEAD + rf' (?:to|about|that) (?P<task>.+?) {WHEN}(?: please)?')
REMIND_BEFORE = re.compile(REMIND_LEAD + rf' {WHEN}(?: (?:to|about|that) (?P<task>.+?))?(?: please)?')
REMIND_ANY = re.compile(REMIND_LEAD + r'\b')
KIND = r'(?P<kind>timers?|reminders?|alarms?|timers and reminders|reminders and timers)'
LIST = re.compile(LEAD + r'(?:(?:what|which) (?:timers?|reminders?|alarms?|timers and reminders) (?:do i have|are (?:there|set|running|on))'
                  r'|(?:list|show me|tell me|read me) (?:all )?(?:of )?(?:my |the )?(?:timers?|reminders?|alarms?|timers and reminders)'
                  r'|what are my (?:timers?|reminders?|alarms?|timers and reminders)'
                  r'|(?:do i have|are there) any (?:timers?|reminders?|alarms?)(?: set| running| on)?'
                  r'|how (?:much|long) (?:time )?(?:is )?(?:left|remaining)(?: on (?:the|my) timers?)?'
                  r'|how long until (?:the|my) timer(?: goes off| is done| finishes)?'
                  r'|whats (?:left )?on (?:the|my) timers?)(?: please)?')
CANCEL = re.compile(LEAD + rf'(?:cancel|stop|delete|remove|clear|turn off) (?P<all>all (?:of )?(?:my |the )?|every )?'
                    rf'(?:(?:the|my) )?(?:(?P<dur2>{DURATION}) timer|{KIND}(?: (?:for|of) (?P<dur>{DURATION})'
                    rf'| (?:to|about|that) (?P<task>.+))?)(?: please)?')

NOT_WHEN = ("When should I remind you? Say remind me in 10 minutes, or remind me at 6 PM, then what to do. "
            "Nothing was set.")


def normalize(raw):
    """Lowercase with only the punctuation times need: "6:30 p.m." -> "6:30 pm"."""
    text = raw.lower()
    text = re.sub(r'\b([ap])\.\s?m\b\.?', r'\1m', text)
    text = re.sub(r"\bo'?\s?clock\b", 'oclock', text)
    text = re.sub(r'(\d)[.:](\d\d)\b', r'\1:\2', text)
    text = re.sub(r'(\d)\s?(am|pm)\b', r'\1 \2', text)
    text = re.sub(r"'", '', text)
    text = re.sub(r'[^a-z0-9:\s]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def number(word):
    if word.isdigit():
        return int(word)
    if word in ('a', 'an'):
        return 1
    tens, _, units = word.partition(' ')
    if tens in TENS:
        return TENS[tens] + (SMALL.index(units) if units else 0)
    return SMALL.index(word)


def duration(text):
    """Seconds in a DURATION match."""
    if text in ('half an hour', 'a half hour'):
        return 1800
    seconds = 0
    for match in re.finditer(rf'(?P<n>{NUMBER}) (?P<unit>{UNIT})(?P<half> and a half)?', text):
        size = {'h': 3600, 'm': 60, 's': 1}[match['unit'][0]]
        seconds += number(match['n']) * size + (size // 2 if match['half'] else 0)
    return seconds


def minute(word):
    if word.isdigit():
        return int(word)
    return number(word[3:]) if word.startswith('oh ') else number(word)


def clock_time(match, now, day=None):
    """The datetime a CLOCK match (plus an optional DAY) means, or None for a time already past today."""
    text = match['clock']
    if text in ('noon', 'midnight'):
        hour, mins, ap = (12 if text == 'noon' else 0), 0, None
    else:
        hour = number(match['h'])
        mins = int(match['m']) if match['m'] else minute(match['mw']) if match['mw'] else 0
        ap = match['ap']
    if hour > 23 or mins > 59:
        return None
    pm = ap in ('pm', 'in the afternoon', 'in the evening', 'at night', 'tonight') or \
        (day or '').endswith(('afternoon', 'evening', 'night', 'tonight'))
    am = ap in ('am', 'in the morning') or (day or '').endswith('morning')
    if text in ('noon', 'midnight') or hour > 12 or hour == 0:
        candidates = [hour]
    elif pm:
        candidates = [hour % 12 + 12]
    elif am:
        candidates = [hour % 12]
    else:
        candidates = [hour % 12, hour % 12 + 12]
    tomorrow = (day or '').startswith('tomorrow')
    base = (now + timedelta(days=1 if tomorrow or text == 'midnight' else 0)).replace(second=0, microsecond=0)
    if tomorrow and len(candidates) == 2:
        # "Tomorrow at 7" means the morning, "tomorrow at 3" the afternoon.
        candidates = [hour % 12] if 7 <= hour <= 11 else [hour % 12 + 12]
    times = [base.replace(hour=h, minute=mins) for h in candidates]
    later = [t for t in times if t > now]
    if later:
        return min(later)
    if day or am or pm or text == 'noon':
        return None  # "at 6 PM today" when it's 7 PM: don't silently move it to tomorrow
    return min(times) + timedelta(days=1)


def when(match, now):
    """(due datetime, None) or (None, the reason it can't be set)."""
    if match['dur']:
        seconds = duration(match['dur'])
        if not 0 < seconds <= MAX_REMINDER_DAYS * 86400:
            return None, f"I can set reminders up to {MAX_REMINDER_DAYS} days ahead. Nothing was set."
        return now + timedelta(seconds=seconds), None
    day = match['day1'] or match['day2'] or match['day3']
    if not match['clock']:
        return None, NOT_WHEN  # "remind me tomorrow": no time
    due = clock_time(match, now, day)
    if due is None:
        return None, "That time has already passed today. Nothing was set."
    return due, None


def spoken_duration(seconds):
    hours, rest = divmod(int(seconds), 3600)
    mins, secs = divmod(rest, 60)
    parts = [f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in ((hours, 'hour'), (mins, 'minute'), (secs, 'second')) if n]
    return ' and '.join(parts) if len(parts) <= 2 else f'{parts[0]}, {parts[1]} and {parts[2]}'


def spoken_left(seconds):
    """Remaining time, rounded the way it's worth saying."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return spoken_duration(seconds - seconds % 60)
    if seconds >= 600:
        return spoken_duration((seconds + 59) // 60 * 60)
    return spoken_duration(seconds) or 'less than a second'


def spoken_clock(due, now):
    text = f"{due:%-I:%M %p}".replace(':00 ', ' ')
    days = (due.date() - now.date()).days
    if days == 1:
        return f'{text} tomorrow'
    if days > 1:
        return f'{text} on {due:%A}'
    return text


def yours(task):
    """The reminder in Eddie's words: "call my mom" -> "call your mom"."""
    swap = {'my': 'your', 'me': 'you', 'i': 'you', 'im': 'youre', 'mine': 'yours', 'myself': 'yourself', 'our': 'your', 'us': 'you'}
    return ' '.join(swap.get(word, word) for word in task.split())


class Timers:
    """Pending timers and reminders: {id, kind, due (epoch s), seconds, task}.

    path None keeps them in memory only; otherwise every change rewrites the file atomically.
    """
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.lock = threading.Lock()
        self.items = []
        self.load_error = None
        if self.path and self.path.exists():
            try:
                items = json.loads(self.path.read_text())
                self.items = [item for item in items if item['kind'] in ('timer', 'reminder')
                              and isinstance(item['due'], (int, float))][:MAX_ITEMS]
            except (OSError, ValueError, TypeError, KeyError) as error:
                self.load_error = f'Timers file {self.path} unreadable ({error}); starting with none.'

    def save(self):
        if not self.path:
            return
        tmp = self.path.with_name(self.path.name + '.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump(self.items, file)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def add(self, kind, due, seconds=None, task=None):
        with self.lock:
            if len(self.items) >= MAX_ITEMS:
                return None
            item = {'id': uuid.uuid4().hex[:8], 'kind': kind, 'due': due, 'seconds': seconds, 'task': task}
            self.items.append(item)
            self.save()
            return item

    def pending(self):
        with self.lock:
            return sorted(self.items, key=lambda item: item['due'])

    def remove(self, ids):
        with self.lock:
            self.items = [item for item in self.items if item['id'] not in ids]
            self.save()

    def take_due(self):
        """Remove and return what's due, oldest first. Removed before ringing, so it never rings twice."""
        with self.lock:
            now = self.clock()
            due = sorted((item for item in self.items if item['due'] <= now), key=lambda item: item['due'])
            if due:
                self.items = [item for item in self.items if item['due'] > now]
                self.save()
            return due


def describe(item, now):
    """One pending item, for list and cancel replies."""
    left = item['due'] - now.timestamp()
    if item['kind'] == 'timer':
        return f"a timer for {spoken_duration(item['seconds'])} with {spoken_left(left)} left"
    due = datetime.fromtimestamp(item['due'])
    what = f" to {yours(item['task'])}" if item['task'] else ''
    return f"a reminder at {spoken_clock(due, now)}{what}"


def join(parts):
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def kind_of(word):
    """'timer', 'reminder' or None for both."""
    if word is None or ' and ' in word or word.startswith('alarm'):
        return None
    return 'reminder' if word.startswith('reminder') else 'timer'


def label(kind):
    return {'timer': 'timers', 'reminder': 'reminders', None: 'timers or reminders'}[kind]


def set_timer(store, seconds, now):
    if not 0 < seconds <= MAX_TIMER:
        return "I can set timers up to 24 hours. Nothing was set."
    if store.add('timer', now.timestamp() + seconds, seconds=seconds) is None:
        return f"You already have {MAX_ITEMS} timers and reminders, so I didn't set another. Cancel one first."
    return f"Timer set for {spoken_duration(seconds)}."


def set_reminder(store, match, now):
    due, problem = when(match, now)
    if problem:
        return problem
    task = (match['task'] or '').strip() or None
    if store.add('reminder', due.timestamp(), task=task) is None:
        return f"You already have {MAX_ITEMS} timers and reminders, so I didn't set another. Cancel one first."
    what = f" to {yours(task)}" if task else ''
    if match['dur']:
        return f"Okay, I'll remind you in {spoken_duration(duration(match['dur']))}{what}."
    return f"Okay, I'll remind you at {spoken_clock(due, now)}{what}."


def list_items(store, now):
    items = store.pending()
    if not items:
        return "You don't have any timers or reminders."
    if len(items) == 1:
        return f"You have {describe(items[0], now)}."
    return f"You have {len(items)}: {join([describe(item, now) for item in items])}."


def cancel(store, match, now):
    kind = 'timer' if match['dur2'] else kind_of(match['kind'])
    items = [item for item in store.pending() if kind is None or item['kind'] == kind]
    seconds = duration(match['dur'] or match['dur2']) if (match['dur'] or match['dur2']) else None
    if seconds is not None:
        items = [item for item in items if item['kind'] == 'timer' and item['seconds'] == seconds]
    if match['task']:
        wanted = set(match['task'].split()) - {'the', 'a', 'an', 'my', 'to'}
        items = [item for item in items if item['task'] and wanted <= set(item['task'].split())]
    if not items:
        if seconds is not None or match['task']:
            return f"I couldn't find that {kind or 'timer'}. Nothing was cancelled."
        return f"You don't have any {label(kind)}."
    if len(items) > 1 and not match['all']:
        which = join([describe(item, now) for item in items])
        return (f"You have {len(items)}: {which}. Say which one, like cancel the timer for 10 minutes, "
                f"or cancel all {label(kind)}. Nothing was cancelled.")
    store.remove({item['id'] for item in items})
    if len(items) > 1:
        return f"Cancelled {len(items)} {label(kind) if kind else 'timers and reminders'}."
    return f"Cancelled {describe(items[0], now)}."


def handle(store, raw, now=None):
    """The spoken reply when raw is a timer or reminder command, else None."""
    text = normalize(raw)
    now = now or datetime.fromtimestamp(store.clock())
    match = TIMER_SET.fullmatch(text)
    if match:
        return set_timer(store, duration(match['dur'] or match['dur2']), now)
    match = REMIND_AFTER.fullmatch(text) or REMIND_BEFORE.fullmatch(text)
    if match:
        return set_reminder(store, match, now)
    if REMIND_ANY.match(text):
        return NOT_WHEN
    if LIST.fullmatch(text):
        return list_items(store, now)
    match = CANCEL.fullmatch(text)
    if match:
        return cancel(store, match, now)
    return None


def announcement(item, now):
    """(dashboard title, what Eddie says) when an item rings."""
    due = datetime.fromtimestamp(item['due'])
    late = now.timestamp() - item['due'] > LATE_SECONDS
    if item['kind'] == 'timer':
        text = f"Your timer for {spoken_duration(item['seconds'])} is done."
        if late:
            text = f"Sorry, I'm late. Your timer for {spoken_duration(item['seconds'])} finished at {spoken_clock(due, now)}."
        return 'Timer done', text
    what = f" to {yours(item['task'])}" if item['task'] else ''
    if late:
        return 'Reminder', f"Sorry, I'm late. This was your reminder for {spoken_clock(due, now)}{what}."
    return 'Reminder', f"Reminder{what}." if what else "This is your reminder."


ALARM_SECONDS = 10  # a custom sound is cut off here


def chime(rate=22050):
    """The default alarm: three rising beeps, three times (about 3.4 s), generated, so nothing to provision."""
    import numpy as np
    t = np.arange(int(rate * .18)) / rate
    envelope = np.sin(np.pi * np.arange(len(t)) / (len(t) - 1)) ** 2
    gap, rest = np.zeros(int(rate * .07)), np.zeros(int(rate * .45))
    beeps = [.25 * np.sin(2 * np.pi * f * t) * envelope for f in (880, 1175, 1568)]
    burst = np.concatenate([beeps[0], gap, beeps[1], gap, beeps[2], rest])
    return np.tile(burst, 3).astype(np.float32), rate


def wav_sound(path):
    """A local 16-bit PCM WAV (EDDIE_ALARM_SOUND), mixed to mono and cut to ALARM_SECONDS."""
    import wave
    import numpy as np
    with wave.open(str(path), 'rb') as file:
        if file.getsampwidth() != 2:
            raise ValueError('EDDIE_ALARM_SOUND must be a 16-bit PCM WAV file')
        rate, channels = file.getframerate(), file.getnchannels()
        frames = file.readframes(min(file.getnframes(), rate * ALARM_SECONDS))
    samples = np.frombuffer(frames, dtype='<i2').astype(np.float32) / 32768
    return samples.reshape(-1, channels).mean(axis=1), rate


def play_alarm(sd=None):
    if sd is None:
        import sounddevice as sd
    path = os.environ.get('EDDIE_ALARM_SOUND')
    samples, rate = wav_sound(path) if path else chime()
    playback.play(sd, samples, rate)
