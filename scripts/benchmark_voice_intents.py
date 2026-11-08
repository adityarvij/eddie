"""Evaluate the provisioned local intent model and measure warm matching latency."""
import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'voice'))
import local_intent


CASES = {
    'device_activity': (
        'what has the television been doing', 'tell me about the chromecast activity',
        'is the roku talking to anyone', 'how busy has my fire stick been', 'what is my television up to',
    ),
    'summary': (
        'catch me up', 'how has the network been today', 'give me the big picture',
        'anything happening around here', 'tell me what is going on overall',
    ),
    'recent_blocks': (
        'what did you stop lately', 'which sites were just blocked', 'show the newest blocked traffic',
        'any recent tracker blocks', 'what have you blocked',
    ),
    'why_blocked': (
        'explain the block on metrics.example.com', 'how come ads.example.org was stopped',
        'what was wrong with tracker.example.net', 'why did you deny telemetry.example.com',
        'tell me the reason example.com got blocked',
    ),
    None: (
        'block the fire stick', 'allow netflix.com', 'turn on home mode', 'undo that',
        'set a timer for five minutes', 'tell me a joke', 'what do companies know about me',
        'when is the house empty', 'stop listening', 'hello', 'is home mode on',
        'should I block the TV', 'email Will the report', 'remind me to call mom',
        'lock down the roku', 'what time is it', 'how private is my network',
        'can you help me', 'why is the sky blue', 'play some music',
    ),
}


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, default=local_intent.MODEL_DIR)
    parser.add_argument('--runs', type=int, default=100, help='Warm matches used for latency (default: 100)')
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error('--runs must be positive')

    started = time.perf_counter()
    matcher = local_intent.Matcher(local_intent.MiniLM(args.model_dir))
    load_ms = (time.perf_counter() - started) * 1000
    misses = []
    total = 0
    correct = 0
    for wanted, phrases in CASES.items():
        for phrase in phrases:
            got = matcher.match(phrase)
            total += 1
            correct += got == wanted
            if got != wanted:
                misses.append((phrase, wanted, got))

    samples = []
    phrases = [phrase for group in CASES.values() for phrase in group]
    for index in range(args.runs):
        started = time.perf_counter()
        matcher.match(phrases[index % len(phrases)])
        samples.append((time.perf_counter() - started) * 1000)

    print(f'quality: {correct}/{total} ({correct / total:.1%})')
    print(f'model load and example embedding: {load_ms:.1f} ms')
    print(f'warm match latency over {args.runs} runs: median {statistics.median(samples):.1f} ms, '
          f'p95 {percentile(samples, .95):.1f} ms')
    for phrase, wanted, got in misses:
        print(f'miss: {phrase!r}: expected {wanted!r}, got {got!r}')
    return 0 if correct / total >= .9 else 1


if __name__ == '__main__':
    raise SystemExit(main())
