"""Local intent model (#179): understands traffic questions without an LLM.

A small sentence-embedding model (all-MiniLM-L6-v2, ONNX, pinned in voice/assets.json) turns the
transcript into a vector and compares it with example phrasings of each intent, so "what do you
think the Fire Stick is up to" and "what's the TV doing" land on the same answer. Device and domain
come from the words themselves, and every answer is built from the traffic API's numbers through
ollama_agent's read tools, so nothing is invented and no language model is called.

Below MIN_SCORE, or within MARGIN of another intent, or closest to an "other" example, it returns
None and the router carries on as before. Transcripts are embedded in memory only; nothing is
logged or stored.
"""
import re
from pathlib import Path

import numpy as np

import ollama_agent
import voice_agent

MODEL_DIR = Path(__file__).resolve().parent / 'models' / 'minilm'
MIN_SCORE = 0.5  # cosine similarity to the nearest example
MARGIN = 0.05  # over the best example of any other intent

EXAMPLES = {
    'activity': [
        "what's the fire stick doing", "what is the tv up to", "what has my phone been doing",
        "is the tv talking to anyone", "what's my tv been sending", "who is the fire stick talking to",
        "what do you think the tv is doing", "how's the tv behaving", "show me what the iphone is doing",
        "what's been going on", "what's happening on the network", "give me an update",
        "how are things looking", "what are my devices doing", "anything going on",
        "summarize the last hour", "what happened today", "how busy is the network",
        "how many requests did the tv make", "how much has my phone been online",
        "is my laptop communicating with anything", "which servers is the speaker contacting",
    ],
    'blocks': [
        "what got blocked recently", "what did you block", "what have you blocked lately",
        "anything blocked", "show me what you blocked", "what did eddie stop",
        "which trackers did you block", "what was blocked on the tv", "did you block anything today",
    ],
    'explain': [
        "why was device metrics us amazon com blocked", "why did you block that", "what is that domain",
        "why is that blocked", "what does that tracker do", "is that a tracker",
        "who owns that domain", "explain that block", "why was it flagged",
    ],
    # Close to the questions above but not answerable from traffic: these must fall through.
    'other': [
        "what time is it", "tell me a joke", "what's the weather", "how are you", "play some music",
        "turn on the lights", "set a timer for ten minutes", "what can you do", "who made you",
        "is my doorbell recording video", "call mom", "what's on tv tonight", "turn the tv off",
        "thank you eddie", "good night eddie", "never mind", "what's my ip address", "how fast is the internet",
    ],
}

WINDOWS = (('15m', re.compile(r'\b(?:15|fifteen) minutes\b|\bfew minutes\b|\bright now\b')),
           ('24h', re.compile(r'\btoday\b|\b(?:24|twenty four) hours\b|\bovernight\b|\byesterday\b|\bday\b')))
# "the <name> doing / up to / been ..." names a device; these words name the whole house instead.
NAMED = re.compile(r'\b(?:the|my|our) ([a-z0-9 ]+?) (?:is |has |been |doing|up to|talking|sending|saying|behaving)')
EVERYTHING = {'network', 'house', 'home', 'devices', 'stuff', 'everything', 'wifi', 'internet', 'things'}
PHONE_WORDS = ('phone', 'iphone', 'pixel', 'galaxy', 'android')
BACKEND_FAILURE = "I can't reach Eddie's traffic backend right now, so I can't say."


class Encoder:
    """all-MiniLM-L6-v2: mean-pooled, unit-length sentence vectors."""

    def __init__(self, directory=MODEL_DIR):
        import onnxruntime
        import tokenizers
        self.tokenizer = tokenizers.Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.tokenizer.enable_truncation(128)
        self.tokenizer.enable_padding()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 2  # leave cores for DNS and Whisper on the Pi
        self.session = onnxruntime.InferenceSession(str(directory / 'model.onnx'), options,
                                                    providers=['CPUExecutionProvider'])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def __call__(self, texts):
        encoded = self.tokenizer.encode_batch(list(texts))
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        feed = {'input_ids': np.array([e.ids for e in encoded], dtype=np.int64), 'attention_mask': mask,
                'token_type_ids': np.array([e.type_ids for e in encoded], dtype=np.int64)}
        hidden = self.session.run(None, {k: v for k, v in feed.items() if k in self.inputs})[0]
        summed = (hidden * mask[..., None]).sum(axis=1) / np.maximum(mask.sum(axis=1, keepdims=True), 1)
        return summed / np.linalg.norm(summed, axis=1, keepdims=True)


class Intents:
    def __init__(self, encoder=None):
        self.encode = encoder or Encoder()
        self.labels = [name for name, phrases in EXAMPLES.items() for _ in phrases]
        self.vectors = self.encode([p for phrases in EXAMPLES.values() for p in phrases])

    def classify(self, text):
        """(intent, score), or (None, score) when no intent is clearly closest."""
        scores = self.vectors @ self.encode([text])[0]
        best = {}
        for label, score in zip(self.labels, scores):
            best[label] = max(best.get(label, -1.0), float(score))
        ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)
        (intent, score), runner_up = ranked[0], ranked[1][1]
        if intent == 'other' or score < MIN_SCORE or score - runner_up < MARGIN:
            return None, score
        return intent, score

    def __call__(self, raw):
        """The spoken answer, or None to let the router carry on."""
        text = ' '.join(re.sub(r'[^a-z0-9\s]', ' ', raw.lower()).split())
        intent, _ = self.classify(text)
        if intent is None:
            return None
        try:
            return ANSWERS[intent](raw, text)
        except ollama_agent.BackendError:
            return BACKEND_FAILURE
        except ollama_agent.ToolError:
            return None  # e.g. a spoken domain that isn't one: let the router answer


def window(text):
    return next((name for name, pattern in WINDOWS if pattern.search(text)), '1h')


def device_in(text, devices):
    """(device names mentioned, the unmatched name if one was clearly meant)."""
    padded = f' {text} '
    found = [d['device'] for d in devices
             if (name := ' '.join(re.sub(r'[^a-z0-9\s]', ' ', d['device'].lower()).split()))
             and f' {name} ' in padded]
    if not found and re.search(r'\b(?:tv|television|telly)\b', text):
        found = [d['device'] for d in devices if any(w in d['device'].lower() for w in ollama_agent.TV_WORDS)]
    if not found and re.search(r'\bphones?\b', text):
        found = [d['device'] for d in devices if any(w in d['device'].lower() for w in PHONE_WORDS)]
    named = NAMED.search(text)
    meant = named[1] if named and not found and named[1] not in EVERYTHING else None
    return found, meant


def unknown(name, devices):
    names = [d['device'] for d in devices]
    known = f" The devices I've seen today are {voice_agent.join(names)}." if names else ''
    return f"I haven't seen a device called {name} today.{known}"


def activity(raw, text):
    span = window(text)
    devices = ollama_agent.read('/summary?window=24h').get('devices', [])
    found, meant = device_in(text, devices)
    if meant:
        return unknown(meant, devices)
    if not found:
        data = ollama_agent.get_summary(span)
        if span == '1h' and data.get('spoken_summary_last_hour'):
            return str(data['spoken_summary_last_hour'])
        rows = sorted(data['devices'], key=lambda d: d['requests'], reverse=True)[:3]
        return ' '.join(d['said'] for d in rows) or f"Nothing's been on the network in the last {ollama_agent.WINDOW_WORDS[span]}."
    for span_tried in dict.fromkeys((span, '24h')):  # quiet in the last hour: say what it did today
        said = [d['said'] for d in ollama_agent.get_summary(span_tried)['devices'] if d['device'] in found]
        if said:
            return ' '.join(said)
    return f"{voice_agent.join(found)} hasn't been on the network today."


def blocks(raw, text):
    devices = ollama_agent.read('/summary?window=24h').get('devices', [])
    found, meant = device_in(text, devices)
    if meant:
        return unknown(meant, devices)
    rows = ollama_agent.get_recent_blocks(found[0] if len(found) == 1 else None, 3)['blocks']
    if not rows:
        return "Eddie hasn't blocked anything recently."
    voice_agent.context['domain'] = rows[0]['domain']  # "block it" / "why" follow-ups mean the newest
    parts = [f"{row['domain']} for {row['device']}, {ago(row['minutes_ago'])}" for row in rows]
    return f"The latest {'block was' if len(parts) == 1 else 'blocks were'} {voice_agent.join(parts)}."


def ago(minutes):
    if minutes < 1:
        return 'just now'
    if minutes < 120:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    return f'{minutes // 60} hours ago'


def explain(raw, text):
    candidates = spoken_domains(raw) or [voice_agent.context.get('domain')]
    if not candidates[0]:
        return "Which domain? Say it with dot, like device metrics us dot amazon dot com."
    results = [ollama_agent.explain_domain(c) for c in candidates]
    result = next((r for r in results if r['seen'] or 'ai_verdict' in r), results[0])
    domain = result['domain']
    voice_agent.context['domain'] = domain
    if not result['seen'] and 'ai_verdict' not in result:
        return f"Eddie hasn't seen {domain}."
    said = []
    last = result.get('last_decision')
    if last:
        verb = {'block': 'blocked', 'allow': 'allowed', 'flag': 'flagged'}.get(last['action'], last['action'])
        owner = f", which belongs to {result['company']}," if result.get('company') else ''
        said.append(f"Eddie {verb} {domain}{owner} for {last['device']} {ago(last['minutes_ago'])}.")
        if last.get('reason'):
            said.append(f"Reason: {last['reason']}.")
    verdict = result.get('ai_verdict')
    if verdict and verdict.get('label'):
        why = f": {verdict['reason']}" if verdict.get('reason') else ''
        said.append(f"The classifier labeled it {verdict['label']}{why}.")
    return ' '.join(said)


def spoken_domains(raw):
    """The domain in a transcript, longest first: speech loses hyphens, so "device metrics us dot
    amazon dot com" could be us.amazon.com or device-metrics-us.amazon.com; the API says which it saw."""
    spoken = re.sub(r'\s+dot\s+', '.', raw.lower().replace("'", ''))
    match = voice_agent.SPOKEN_DOMAIN.search(spoken)
    if not match:
        return []
    before = re.sub(r'[^a-z0-9\s-]', ' ', spoken[:match.start()]).split()
    joined = [f"{'-'.join(before[-n:])}-{match[0]}" for n in range(min(3, len(before)), 0, -1)]
    return [d for d in joined if voice_agent.DOMAIN.fullmatch(d)] + [match[0]]


ANSWERS = {'activity': activity, 'blocks': blocks, 'explain': explain}
