"""Offline GeoIP installer (#102): fetch the pinned country database once and verify its SHA-256.

    python scripts/provision_geoip.py                  # download if missing, then verify
    python scripts/provision_geoip.py --verify         # no network: exit 1 if missing or altered
    python scripts/provision_geoip.py --from FILE.gz   # copy a file fetched elsewhere; same pinned hash

The pin (url, size, sha256) is eddie/data/geoip.json. Never overwrites a file whose hash is
wrong; delete it and run again. The DNS server never downloads; this script is the only place
the database is fetched.
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from provision_voice import AssetError, download  # noqa: E402

from eddie import geoip  # noqa: E402


def target_of(pin: dict, root: Path = geoip.ROOT) -> Path:
    return root / pin["path"]


def problem(path: Path, pin: dict) -> str | None:
    if not path.is_file():
        return "missing"
    actual = geoip.sha256(path)
    return None if actual == pin["sha256"] else f"SHA-256 mismatch: expected {pin['sha256']}, got {actual}"


def provision(pin: dict, root: Path = geoip.ROOT, source: Path | None = None, log=print) -> Path:
    """Fetch (or copy) only when missing; refuse to replace a file with the wrong hash."""
    target = target_of(pin, root)
    found = problem(target, pin)
    if found is None:
        log(f"{target}: ok")
        return target
    if found != "missing":
        raise AssetError(f"{target}: {found}. Not overwriting; delete it and run again.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source is not None:
        part = target.with_name(target.name + ".part")
        shutil.copyfile(source, part)
        found = problem(part, pin)
        if found:
            part.unlink()
            raise AssetError(f"{source}: {found}")
        part.replace(target)
    else:
        log(f"fetching {pin['url']} ({pin['source']}, {pin['license']})")
        download(pin["url"], target, pin["sha256"], pin.get("size"))
    log(f"{target}: ok")
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="hash the database only; never uses the network")
    mode.add_argument("--from", dest="source", type=Path, help="copy this already-downloaded file instead of fetching")
    parser.add_argument("--manifest", type=Path, default=geoip.MANIFEST)
    args = parser.parse_args(argv)
    pin = geoip.manifest(args.manifest)
    if args.verify:
        found = problem(target_of(pin), pin)
        if found:
            print(f"GEOIP FAILED {target_of(pin)}: {found}", file=sys.stderr)
            print("Run python scripts/provision_geoip.py while online.", file=sys.stderr)
            return 1
        print(f"Verified {target_of(pin)} ({pin['source']}).")
        return 0
    try:
        provision(pin, source=args.source)
    except (AssetError, OSError) as exc:
        print(f"PROVISIONING FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
