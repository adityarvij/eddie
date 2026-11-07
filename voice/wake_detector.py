"""Interchangeable local wake engines. Prototype mode must be selected explicitly."""
import json
import re

# Unanchored so a wake mid-conversation counts; bare "ed" is excluded because "hey Ed" is ordinary speech.
WAKE = re.compile(r'\b(?:hey|hi|okay)\s+(?:eddie|eddy|edie)\b', re.I)


def matches_phrase(text):
    return bool(WAKE.search(text))


class VoskWake:
    def __init__(self, path):
        from vosk import Model, KaldiRecognizer, SetLogLevel
        SetLogLevel(-1)
        self.model = Model(str(path))
        # Unrestricted decoder avoids forcing unrelated speech into the wake phrase.
        self.recognizer = KaldiRecognizer(self.model, 16000)

    def predict(self, frame):
        final = self.recognizer.AcceptWaveform(frame.tobytes())
        result = json.loads(self.recognizer.Result() if final else self.recognizer.PartialResult())
        text = result.get('text' if final else 'partial', '')
        return {'hey_eddie_prototype': float(matches_phrase(text))}

    def reset(self):
        self.recognizer.Reset()
