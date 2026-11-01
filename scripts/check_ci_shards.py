"""Verify that four successful Windows pytest shards cover one complete collection."""

import argparse
import hashlib
import json
from pathlib import Path
import sys


class ShardValidationError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise ShardValidationError(message)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Receipt contains duplicate JSON keys")
        result[key] = value
    return result


def _collection_hash(nodeids):
    raw = json.dumps(sorted(nodeids), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _assignment(nodeid):
    return int.from_bytes(hashlib.sha256(nodeid.encode("utf-8")).digest(), "big") % 4


def _validate_pytest_args(args):
    _require(isinstance(args, list) and all(isinstance(arg, str) for arg in args),
             "Receipt has invalid pytest arguments")
    # Receipts must prove the default full suite, without selectors or collect-only runs.
    switches = {"-q", "--quiet", "-v", "--verbose", "-ra", "-rA", "--disable-warnings"}
    value_options = {"--basetemp", "--junitxml", "--junit-xml", "--tb", "--capture", "--color"}
    position = 0
    while position < len(args):
        arg = args[position]
        key, separator, value = arg.partition("=")
        if arg in switches:
            position += 1
        elif key in value_options and separator and value:
            position += 1
        elif (arg in value_options and position + 1 < len(args) and args[position + 1]
              and not args[position + 1].startswith("-")):
            position += 2
        else:
            raise ShardValidationError("Receipt has filtering or unsupported pytest arguments")


def _nodeids(value, field):
    _require(isinstance(value, list) and all(isinstance(item, str) and item for item in value),
             f"Receipt has invalid {field}")
    _require(len(value) == len(set(value)), f"Receipt has duplicate {field}")
    return set(value)


def _load_receipt(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ShardValidationError("Receipt is unreadable or invalid JSON") from exc
    _require(isinstance(value, dict), "Receipt must be a JSON object")
    _require(value.get("schema_version") == "pytest-shard-v1", "Receipt schema is unsupported")
    for field in ("shard_index", "shard_count", "total_count", "selected_count",
                  "deselected_count", "exit_code", "tests_failed"):
        _require(type(value.get(field)) is int, f"Receipt has invalid {field}")
    _require(value["shard_count"] == 4 and 0 <= value["shard_index"] < 4,
             "Receipt has invalid shard coordinates")
    _require(value.get("collection_completed") is True, "Receipt has incomplete collection")
    _require(value["exit_code"] == 0 and value["tests_failed"] == 0,
             "Receipt reports an unsuccessful pytest run")
    _validate_pytest_args(value.get("pytest_args"))
    full = _nodeids(value.get("full_nodeids"), "full_nodeids")
    selected = _nodeids(value.get("selected_nodeids"), "selected_nodeids")
    _require(bool(full) and value["total_count"] == len(full), "Receipt has invalid total count")
    _require(value["selected_count"] == len(selected)
             and value["deselected_count"] == len(full) - len(selected),
             "Receipt has invalid selection counts")
    _require(value.get("full_collection_hash") == _collection_hash(value["full_nodeids"]),
             "Receipt collection hash does not match its nodeids")
    expected = {nodeid for nodeid in full if _assignment(nodeid) == value["shard_index"]}
    _require(selected == expected, "Receipt selected items do not match the shard assignment")
    return value, full, selected


def check_shards(root):
    root = Path(root)
    _require(root.is_dir(), "Artifact root is not a directory")
    paths = sorted(root.rglob("shard.json"))
    _require(len(paths) == 4, "Expected exactly four shard receipts")
    receipts = [_load_receipt(path) for path in paths]
    indices = [receipt[0]["shard_index"] for receipt in receipts]
    _require(set(indices) == set(range(4)), "Shard indices are missing or duplicated")
    first, full, _ = receipts[0]
    combined = set()
    selected_counts = [0] * 4
    for receipt, candidate, selected in receipts:
        _require(candidate == full and receipt["full_collection_hash"] == first["full_collection_hash"],
                 "Shard collections do not match")
        _require(not combined.intersection(selected), "Shard selections overlap")
        combined.update(selected)
        selected_counts[receipt["shard_index"]] = len(selected)
    _require(combined == full, "Shard selections do not cover the full collection")
    return {"shards": 4, "total_count": len(full), "selected_counts": selected_counts,
            "full_collection_hash": first["full_collection_hash"], "partition_verified": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Downloaded Windows shard report directory")
    args = parser.parse_args(argv)
    try:
        result = check_shards(args.root)
    except ShardValidationError as exc:
        print(f"CI shard validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
