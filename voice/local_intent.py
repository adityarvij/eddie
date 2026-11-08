"""Fast, offline semantic routing for factual voice questions.

The model only selects one of four read-only intents.  Slots still use the
existing deterministic device/domain parsers and every spoken fact comes from
the API tool result.  Model files are provisioned ahead of time; this module
never downloads anything.
"""
from pathlib import Path
import re

import numpy as np


MODEL_DIR = Path(__file__).with_name('models') / 'all-MiniLM-L6-v2'
THRESHOLD = 0.56
NAME = re.compile(r'\b(?:(?:hey|hi|ok|okay)\s+)?(?:eddie|eddy|edie)\b', re.I)
EXAMPLES = {
    'device_activity': (
        "what's the TV doing", "what's my TV been up to",
        'what do you think the Fire Stick is doing', 'show me the Roku activity',
        'tell me about the Chromecast traffic', 'how active has the television been',
        'is my streaming device talking to the internet', 'what has the set top box contacted',
        'is my phone talking to anyone', 'what is my laptop connecting to',
    ),
    'summary': (
        "what's been going on", 'give me an overall summary',
        'how is the network doing', 'catch me up on the house',
        'give me the big picture for my network', 'has anything happened today',
        'tell me the highlights', 'what should I know about the network', 'give me a broad overview',
        'show me the whole picture',
        "what's new", 'any updates', 'how are things at home', 'give me a quick recap',
        'what happened recently', 'anything going on lately', 'how is everything in the house',
        'summarize what happened today', "what's up", "what's the word", "what's new with the house",
    ),
    'recent_blocks': (
        'what got blocked recently', 'show me the latest blocks',
        'which trackers did you just stop', 'recent blocked requests',
        'what sites have you stopped lately', 'show the newest denied traffic',
        'have you blocked any trackers recently', 'list the last things you blocked',
        'which websites did you block most recently',
        'did you stop anything recently', 'which sites got blocked', 'what domains have you blocked',
        'did Eddie block anything today',
    ),
    'why_blocked': (
        'why was example.com blocked', 'why did you block that domain',
        'explain why ads.example.com was stopped', 'what made that tracker get blocked',
        'explain the block on example.com', 'why did you deny telemetry.example.com',
        'what was wrong with tracker.example.com', 'give me the reason that domain was blocked',
    ),
    # Negative anchors make the similarity threshold useful near neighboring
    # commands and question families. They never become a routable intent.
    None: (
        'block the Fire Stick', 'lock down the Roku', 'allow netflix.com',
        'turn on home mode', 'undo that', 'set a timer for five minutes',
        'what do companies know about me', 'when does the house look empty',
        'give me privacy advice', 'how private is my network',
        'tell me a joke', 'what time is it',
        # TV control and listings share "TV" with device questions but aren't about traffic (#192).
        'switch off the television', 'turn the TV on', "what's on television this evening",
        'what channel is the game on', 'play Netflix on the TV', 'turn up the volume',
        "what's the weather", 'thank you Eddie', 'good night',
    ),
}


def _normalize(rows):
    rows = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.maximum(norms, 1e-12)


class MiniLM:
    """Minimal tokenizer + ONNX mean-pooling sentence encoder."""
    def __init__(self, model_dir=MODEL_DIR):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = Path(model_dir)
        self.tokenizer = Tokenizer.from_file(str(model_dir / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=128)
        self.tokenizer.enable_padding(length=None)
        self.session = ort.InferenceSession(str(model_dir / 'model.onnx'), providers=['CPUExecutionProvider'])

    def encode(self, texts):
        encoded = self.tokenizer.encode_batch(list(texts))
        width = max(len(item.ids) for item in encoded)
        ids = np.asarray([item.ids + [0] * (width - len(item.ids)) for item in encoded], dtype=np.int64)
        mask = np.asarray([item.attention_mask + [0] * (width - len(item.attention_mask))
                           for item in encoded], dtype=np.int64)
        types = np.asarray([item.type_ids + [0] * (width - len(item.type_ids)) for item in encoded], dtype=np.int64)
        inputs = {'input_ids': ids, 'attention_mask': mask}
        if any(item.name == 'token_type_ids' for item in self.session.get_inputs()):
            inputs['token_type_ids'] = types
        tokens = self.session.run(None, inputs)[0]
        pooled = (tokens * mask[..., None]).sum(axis=1) / np.maximum(mask.sum(axis=1, keepdims=True), 1)
        return _normalize(pooled)


class Matcher:
    def __init__(self, encoder=None, threshold=THRESHOLD):
        self.encoder = encoder or MiniLM()
        self.threshold = threshold
        self.intents = [intent for intent, examples in EXAMPLES.items() for _ in examples]
        phrases = [phrase for examples in EXAMPLES.values() for phrase in examples]
        self.examples = self.encoder.encode(phrases)

    def match(self, text):
        # The name carries no intent, and "Eddie" pulls "what's up, Eddie" toward "thank you Eddie".
        text = NAME.sub(' ', text).strip(' ,.?!') or text
        scores = self.examples @ self.encoder.encode([text])[0]
        best = int(np.argmax(scores))
        return self.intents[best] if float(scores[best]) >= self.threshold else None


def answer(matcher, text, domain_context=None):
    """Return a factual answer for a matched intent, or None to fall through."""
    intent = matcher.match(text)
    if intent is None:
        return None
    # Imported lazily so provisioning checks and the command router do not load ONNX.
    import ollama_agent
    import voice_agent
    denied = voice_agent.profile.check('traffic')  # every intent reads traffic (#163)
    if denied:
        return denied
    try:
        if intent == 'summary':
            return ollama_agent.get_summary('1h').get('spoken_summary_last_hour')
        if intent == 'device_activity':
            # Keep the existing find_device aliases: a spoken "TV" maps to a
            # Fire Stick/Roku/Chromecast, while a named label can be found
            # inside the whole transcript.
            device = 'TV' if re.search(r'\b(?:tv|television|telly)\b', text, re.I) else text
            rows = ollama_agent.get_summary('1h', device).get('devices', [])
            return ' '.join(row['said'] for row in rows) if rows else None
        if intent == 'recent_blocks':
            result = ollama_agent.get_recent_blocks(limit=5)
            return result.get('said')
        domain = next(iter(ollama_agent.DOMAIN.findall(text.lower())), None) or domain_context
        if not domain:
            return None
        return ollama_agent.explain_domain(domain).get('said')
    except (ollama_agent.BackendError, ollama_agent.ToolError, OSError):
        return ollama_agent.BACKEND_FAILURE

