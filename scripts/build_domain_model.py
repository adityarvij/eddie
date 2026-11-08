"""Trains eddie/data/domain_model.json, the classifier's domain-name model.

Setup-time only: Eddie never downloads or trains at runtime. Fetch the
Cisco Umbrella popularity list yourself (real DNS hostnames, ranked):

    curl -O http://s3-us-west-1.amazonaws.com/umbrella-static/top-1m.csv.zip && unzip top-1m.csv.zip
    uv run --with scikit-learn python scripts/build_domain_model.py top-1m.csv

Labels:
- tracker: every host on blocklists/*.txt, plus Umbrella hosts a blocklist covers.
- needed: every other Umbrella host, and the allowlist. Many of these are
  unlisted trackers (Quantcast, Hotjar, Adobe's smetrics.* hosts), so the
  measured tracker precision is a lower bound; see the hand audit in
  docs/ARCHITECTURE.md.
Network plumbing (decision.is_plumbing) is left out, as in the live queue.

Hosts are split 80/10/10 by registered domain, so validation and test only
hold companies the model never saw: the case that reaches the classifier,
since listed hosts are blocked before it. Model choice and the "needed"
threshold are judged on validation Umbrella hosts (the runtime population);
the "tracker" threshold is TRACKER_AT, set by hand-auditing samples above
candidate thresholds, because the labels can't measure it. Test is reported once. The shipped model is then refit on
all hosts with those settings and the report is stored in the model's meta.
"""
import argparse
import csv
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eddie import decision, domain_model, lists  # noqa: E402

TRACKER_AT = 0.6  # hand-audited, see the module docstring
NEEDED_PRECISION = 0.99  # share of "needed" answers that must be unlisted hosts
PRUNE = 0.1  # weights smaller than this are dropped from the shipped model
MIN_DF = 5  # features on fewer hosts are left out
C_GRID = (0.01, 0.03, 0.1)
DNS_WEIGHTS = (3.0,)  # sample weight of Umbrella hosts against blocklist-only hosts


def dataset(umbrella: Path):
    allow, blocked = lists.load()
    rows = {}  # host -> (label, from_umbrella)
    with open(umbrella, newline="") as f:
        for _rank, host in csv.reader(f):
            host = host.lower().rstrip(".")
            if not lists.DOMAIN.fullmatch(host) or decision.is_plumbing(host):
                continue
            listed = decision.matches(host, blocked) and not decision.matches(host, allow)
            rows[host] = (1 if listed else 0, True)
    for host in blocked:
        if not decision.is_plumbing(host) and not decision.matches(host, allow):
            rows.setdefault(host, (1, False))
    for host in allow:
        rows[host] = (0, False)
    return rows


def split(host: str) -> str:
    ext = domain_model._EXTRACT(host)
    key = ext.top_domain_under_public_suffix or host
    bucket = hashlib.sha1(key.encode()).digest()[0] % 10
    return "test" if bucket == 0 else "val" if bucket == 1 else "train"


def threshold_needed(scores, labels):
    """Highest threshold under which at most 1% of hosts are listed trackers."""
    pairs = sorted(zip(scores, labels))
    best, fp = 0.0, 0
    for i, (p, y) in enumerate(pairs, 1):
        fp += y
        if 1 - fp / i >= NEEDED_PRECISION:
            best = p
    return best


def metrics(scores, labels, bares, tracker_at, needed_at):
    """Shares and precision against the list labels, applying DomainModel.verdict's rules."""
    pos = sum(labels)
    tracked = [(p, y) for p, y, b in zip(scores, labels, bares) if p >= tracker_at and not b]
    needed = [(p, y) for p, y in zip(scores, labels) if p <= needed_at]
    tp = sum(y for _, y in tracked)
    return {
        "hosts": len(labels), "listed": pos,
        "tracker_precision": round(tp / len(tracked), 3) if tracked else None,
        "tracker_recall": round(tp / pos, 3) if pos else None,
        "needed_precision": round(1 - sum(y for _, y in needed) / len(needed), 4) if needed else None,
        "needed_share": round(len(needed) / len(labels), 3),
        "unsure_share": round(1 - (len(tracked) + len(needed)) / len(labels), 3),
    }


def average_precision(scores, labels):
    pairs = sorted(zip(scores, labels), reverse=True)
    tp, total, pos = 0, 0.0, sum(labels)
    for i, (_, y) in enumerate(pairs, 1):
        if y:
            tp += 1
            total += tp / i
    return total / pos if pos else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("umbrella", type=Path, help="Umbrella top-1m.csv")
    parser.add_argument("--out", type=Path, default=domain_model.MODEL_PATH)
    parser.add_argument("--show", type=int, default=0, help="print this many top-scored unlisted DNS hosts")
    args = parser.parse_args()

    import numpy as np
    from sklearn.feature_extraction.text import CountVectorizer
    from sklearn.linear_model import LogisticRegression

    started = time.time()
    rows = dataset(args.umbrella)
    hosts = sorted(rows)
    y = np.array([rows[h][0] for h in hosts])
    natural = np.array([rows[h][1] for h in hosts])
    part = np.array([split(h) for h in hosts])
    bares = np.array([domain_model.bare(h) for h in hosts])
    # Vocabulary from every host's features (no labels), so each fit below only slices rows.
    vec = CountVectorizer(analyzer=domain_model.features, binary=True, min_df=MIN_DF, dtype=np.float32)
    x = vec.fit_transform(hosts).tocsr()
    names = vec.get_feature_names_out()
    print(f"{len(hosts)} hosts, {y.sum()} listed, {len(names)} features ({time.time() - started:.0f}s)", flush=True)

    def fit(mask, c, dns_weight):
        model = LogisticRegression(C=c, solver="liblinear", max_iter=300)
        model.fit(x[mask], y[mask], sample_weight=np.where(natural[mask], dns_weight, 1.0))
        w = model.coef_[0].copy()
        w[np.abs(w) < PRUNE] = 0.0
        return w, float(model.intercept_[0])

    def scores(w, b, mask):
        z = np.clip(x[mask] @ w + b, -30, 30)
        return [float(v) for v in 1 / (1 + np.exp(-z))], [int(v) for v in y[mask]]

    # Thresholds and model choice are judged on real DNS hostnames, the runtime population.
    val, test = (part == "val") & natural, (part == "test") & natural
    best = None
    for c in C_GRID:
        for dns_weight in DNS_WEIGHTS:
            w, b = fit(part == "train", c, dns_weight)
            s, labels = scores(w, b, val)
            ap = average_precision(s, labels)
            print(f"C={c} dns_weight={dns_weight}: DNS validation AP {ap:.4f}, {np.count_nonzero(w)} weights", flush=True)
            if best is None or ap > best[0]:
                best = (ap, c, dns_weight, w, b, s, labels)
    ap, c, dns_weight, w, b, s, labels = best
    tracker_at, needed_at = TRACKER_AT, threshold_needed(s, labels)
    report = {"C": c, "dns_weight": dns_weight, "dns_validation_average_precision": round(ap, 4),
              "dns_validation": metrics(s, labels, bares[val], tracker_at, needed_at)}
    s, labels = scores(w, b, test)
    report["dns_test"] = metrics(s, labels, bares[test], tracker_at, needed_at)
    report["dns_test_average_precision"] = round(average_precision(s, labels), 4)
    s, labels = scores(w, b, part == "test")
    report["all_test"] = metrics(s, labels, bares[part == "test"], tracker_at, needed_at)
    print(json.dumps(report, indent=1), flush=True)
    if args.show:
        test_hosts = [h for h, m in zip(hosts, test) if m]
        ranked = sorted(zip(scores(w, b, test)[0], test_hosts, y[test]), reverse=True)
        print("top-scored DNS test hosts (1 = listed):")
        for p, h, label in ranked[:args.show]:
            print(f"  {p:.3f} {label} {h}")

    w, b = fit(np.ones(len(hosts), bool), c, dns_weight)
    data = {"bias": round(b, 4),
            "thresholds": {"tracker": round(tracker_at, 4), "needed": round(needed_at, 4)},
            "meta": {"built": time.strftime("%Y-%m-%d"), "dns_hosts": int(natural.sum()),
                     "hosts": len(hosts), "report": report},
            "weights": {str(f): round(float(v), 3) for f, v in zip(names, w) if v}}
    args.out.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(f"{len(data['weights'])} weights -> {args.out} ({args.out.stat().st_size // 1024} KB, "
          f"{time.time() - started:.0f}s)")


if __name__ == "__main__":
    main()
