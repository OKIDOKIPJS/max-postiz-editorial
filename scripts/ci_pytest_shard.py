"""Run one deterministic pytest partition and preserve pytest's actual exit code."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

import pytest


def shard_for(nodeid, count):
    return int.from_bytes(hashlib.sha256(nodeid.encode("utf-8")).digest(), "big") % count


def collection_hash(nodeids):
    # Sorted JSON keeps multiplicity and makes the receipt independent of collection order.
    raw = json.dumps(sorted(nodeids), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ShardReceipt:
    def __init__(self, index, count, path, pytest_args):
        self.index = index
        self.count = count
        self.path = Path(path)
        self.receipt = {
            "schema_version": "pytest-shard-v1",
            "algorithm": "sha256(utf8(nodeid)) % shard_count",
            "shard_index": index,
            "shard_count": count,
            "pytest_args": list(pytest_args),
            "collection_completed": False,
            "full_collection_hash": None,
            "full_nodeids": [],
            "selected_nodeids": [],
            "total_count": 0,
            "selected_count": 0,
            "deselected_count": 0,
            "exit_code": None,
        }

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        full_nodeids = [item.nodeid for item in items]
        selected, deselected = [], []
        for item in items:
            target = selected if shard_for(item.nodeid, self.count) == self.index else deselected
            target.append(item)
        items[:] = selected
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        self.receipt.update(
            collection_completed=True,
            full_collection_hash=collection_hash(full_nodeids),
            full_nodeids=full_nodeids,
            selected_nodeids=[item.nodeid for item in selected],
            total_count=len(full_nodeids),
            selected_count=len(selected),
            deselected_count=len(deselected),
        )
        self.write()

    def pytest_sessionfinish(self, session, exitstatus):
        self.receipt.update(exit_code=int(exitstatus), tests_failed=session.testsfailed)
        self.write()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--shard-receipt", type=Path)
    args, pytest_args = parser.parse_known_args(argv)
    if args.shard_count < 1:
        parser.error("--shard-count must be positive")
    if not 0 <= args.shard_index < args.shard_count:
        parser.error("--shard-index must be in [0, --shard-count)")
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    path = args.shard_receipt or Path(
        f".test-state/pytest-shard-{args.shard_index}-of-{args.shard_count}.json"
    )
    receipt = ShardReceipt(args.shard_index, args.shard_count, path, pytest_args)
    receipt.write()
    # Direct script execution starts at scripts/, unlike python -m pytest.
    # Keep the caller's project root importable for namespace helpers as well.
    working_directory = str(Path.cwd())
    if not sys.path or sys.path[0] != working_directory:
        sys.path.insert(0, working_directory)
    # Failed tests, collection errors, interrupts and no-tests exit codes remain failures.
    result = int(pytest.main(pytest_args, plugins=[receipt]))
    # Usage errors can happen before pytest_sessionfinish is called.
    receipt.receipt["exit_code"] = result
    receipt.write()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
