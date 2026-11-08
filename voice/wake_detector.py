"""Interchangeable local wake engines. Prototype mode must be selected explicitly."""
import json
import re

# Unanchored so a wake mid-conversation counts; bare "ed" is excluded because "hey Ed" is ordinary speech.
# "andy" and "edits" are how Vosk's small model heard "hey Eddie" through the Pi's speakerphone.
WAKE = re.compile(r'\b(?:hey|hi|okay)\s+(?:eddie|eddy|edie|edits|andy)\b', re.I)

GRAMMAR = ['hey eddie', 'hey eddy', '[unk]']


def matches_phrase(text):
    return bool(WAKE.search(text))


class VoskWake:
    def __init__(self, path):
        from vosk import Model, KaldiRecognizer, SetLogLevel
        SetLogLevel(-1)
        self.model = Model(str(path))
        # Restricted to the wake phrase plus [unk] for everything else. On the Pi's speakerphone this caught
        # 9 of about 10 "hey Eddie"s against 6 for free transcription, with no wake in 30 s of other talk,
        # and it means the room's speech is never written out as words.
        self.recognizer = KaldiRecognizer(self.model, 16000, json.dumps(GRAMMAR))
        self.held = 0

    def predict(self, frame):
        final = self.recognizer.AcceptWaveform(frame.tobytes())
        result = json.loads(self.recognizer.Result() if final else self.recognizer.PartialResult())
        text = result.get('text' if final else 'partial', '')
        # Vosk can show the phrase on a single frame: only in its final result, or briefly before revising
        # "hey eddie" to "hey edits". Hold a match for this frame and the next, so the runtime's two-frame
        # rule (made for openWakeWord's scores) still sees it.
        if matches_phrase(text):
            self.held = 2
        score = float(self.held > 0)
        self.held = max(0, self.held - 1)
        return {'hey_eddie_prototype': score}

    def reset(self):
        self.recognizer.Reset()
        self.held = 0
