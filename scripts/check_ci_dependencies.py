"""Сверяем доступный Dependabot манифест с точными зависимостями проверок CI."""

import argparse
from pathlib import Path
import re
import sys


LOCK_FILES = (
    "requirements.lock",
    "requirements-dev.lock",
    "requirements-browser.lock",
    "requirements-deployment.lock",
)
CI_MANIFEST = "requirements-ci.txt"
# Только отдельный пакет с точной версией: без ranges, URL, extras, markers и -r.
PIN = re.compile(
    r"(?P<name>[a-z0-9]+(?:[._-][a-z0-9]+)*)=="
    r"(?P<version>(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*"
    r"(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
    r"(?:\+[a-z0-9]+(?:[._-][a-z0-9]+)*)?)",
    re.IGNORECASE,
)


class DependencyCheckError(ValueError):
    """Изменение зависимостей требует согласованной правки манифеста и locks."""


def read_pins(root: Path, filename: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    try:
        lines = (root / filename).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise DependencyCheckError(f"Cannot read {filename}: {type(error).__name__}") from None
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if match is None:
            raise DependencyCheckError(f"{filename}:{number}: expected package==exact.version")
        # PyPI считает регистр и группы дефисов, точек и подчёркиваний равнозначными.
        name = re.sub(r"[-_.]+", "-", match["name"]).lower()
        version = match["version"]
        if name in pins:
            detail = "duplicate pin" if pins[name] == version else "conflicting pins"
            raise DependencyCheckError(f"{filename}:{number}: {detail} for {name}")
        pins[name] = version
    if not pins:
        raise DependencyCheckError(f"{filename}: no pinned dependencies")
    return pins


def check(root: Path) -> int:
    locked: dict[str, str] = {}
    origins: dict[str, str] = {}
    for filename in LOCK_FILES:
        for name, version in read_pins(root, filename).items():
            # Общий пакет в разных locks допустим только с одной точной версией.
            if name in locked and locked[name] != version:
                raise DependencyCheckError(
                    f"{filename}: conflicting pin for {name} with {origins[name]}"
                )
            locked[name] = version
            origins[name] = filename
    manifest = read_pins(root, CI_MANIFEST)
    errors = []
    missing = sorted(locked.keys() - manifest.keys())
    extra = sorted(manifest.keys() - locked.keys())
    changed = sorted(name for name in locked.keys() & manifest.keys()
                     if locked[name] != manifest[name])
    if missing:
        errors.append("missing pins: " + ", ".join(missing))
    if extra:
        errors.append("unexpected pins: " + ", ".join(extra))
    if changed:
        errors.append("version mismatch: " + ", ".join(changed))
    if errors:
        raise DependencyCheckError(f"{CI_MANIFEST}: " + "; ".join(errors))
    return len(locked)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Project directory; defaults to the directory containing this script's parent")
    arguments = parser.parse_args()
    try:
        count = check(arguments.root)
    except DependencyCheckError as error:
        print(f"CI dependency check failed: {error}", file=sys.stderr)
        return 1
    print(f"CI dependency manifest matches {count} exact pins from {len(LOCK_FILES)} lock files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
