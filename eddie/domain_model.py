"""Small local model that labels an unknown domain from its name alone.

A logistic regression over the hostname's labels, words and character
n-grams, trained by scripts/build_domain_model.py on the shipped blocklists
(trackers) against real DNS hostnames that no list blocks (needed). The
weights ship in eddie/data/domain_model.json; nothing is downloaded or
trained at runtime, and scoring a domain takes well under a millisecond.

The model only sees the domain name. It can't know what a host really does,
so a "tracker" verdict means "named like the hosts the blocklists block",
and the reason says so. Below the confidence thresholds it answers "unsure".
"""
import json
import math
import re
from functools import lru_cache
from pathlib import Path

import tldextract

MODEL_PATH = Path(__file__).parent / "data" / "domain_model.json"
# Same offline extractor as labels.py: no network fetch, no cache writes.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)
_WORDS = re.compile(r"[a-z]{2,}")
NGRAMS = (3, 4, 5)


def features(host: str) -> list[str]:
    """The model's input features for a hostname. Training and scoring both use this."""
    host = host.lower().rstrip(".")
    ext = _EXTRACT(host)
    sub = [label for label in ext.subdomain.split(".") if label]
    name = ext.domain or host
    out = [f"s:{ext.suffix}", f"d:{name}.{ext.suffix}", f"n:{min(len(sub), 4)}",
           f"len:{min(len(host) // 10, 6)}", f"dig:{any(c.isdigit() for c in ext.subdomain)}"]
    if sub:
        out.append(f"f:{sub[0]}")
        out += [f"l:{label}" for label in sub]
        out += [f"w:{word}" for word in _WORDS.findall(ext.subdomain)]
        out += _grams("c", ".".join(sub))
    out += [f"dw:{word}" for word in _WORDS.findall(name)]
    out += _grams("dc", name)
    return list(dict.fromkeys(out))


def bare(host: str) -> bool:
    """True for a site name with no subdomain but www."""
    return _EXTRACT(host.lower().rstrip(".")).subdomain in ("", "www")


def _grams(prefix: str, text: str) -> list[str]:
    text = f"^{text}$"
    return [f"{prefix}:{text[i:i + n]}" for n in NGRAMS for i in range(len(text) - n + 1)]


class DomainModel:
    def __init__(self, data: dict):
        self.bias = data["bias"]
        self.weights = data["weights"]
        self.tracker_at = data["thresholds"]["tracker"]
        self.needed_at = data["thresholds"]["needed"]

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> "DomainModel":
        return cls(json.loads(Path(path).read_text()))

    def score(self, host: str) -> float:
        """Probability that the host is named like a blocklisted tracker."""
        z = self.bias + sum(self.weights.get(f, 0.0) for f in features(host))
        return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))

    def evidence(self, host: str, sign: int, limit: int = 2) -> list[str]:
        """Up to limit readable words from the name that pushed the score toward sign (+1 tracker, -1 needed)."""
        words = [(self.weights.get(f, 0.0) * sign, f.split(":", 1)[1]) for f in features(host)
                 if f.startswith(("w:", "dw:"))]
        return [word for weight, word in sorted(words, reverse=True) if weight > 0.3][:limit]

    def verdict(self, host: str) -> dict:
        """The classifier contract: label, confidence and a short reason (<15 words).

        A bare site name (no subdomain, or only www) is never called a tracker:
        on held-out hosts those calls were mostly shops on suffixes that scam
        domains on the blocklists share, not trackers.
        """
        p = self.score(host)
        if p >= self.tracker_at and not bare(host):
            words = self.evidence(host, +1)
            reason = "Name resembles blocklisted trackers" + (f": {', '.join(words)}" if words else "")
            return {"label": "tracker", "confidence": round(p, 2), "reason": reason}
        if p <= self.needed_at:
            return {"label": "needed", "confidence": round(1 - p, 2),
                    "reason": "Name resembles common hosts no blocklist blocks"}
        return {"label": "unsure", "confidence": 0.5,
                "reason": "Name alone doesn't show what it is"}


@lru_cache(maxsize=1)
def default() -> DomainModel:
    return DomainModel.load()
