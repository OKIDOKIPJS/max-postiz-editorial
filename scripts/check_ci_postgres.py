"""Read-only preflight of two explicitly owned PostgreSQL 17 test instances."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def contract_module():
    path = ROOT / "tests/pg_test_contract.py"
    spec = importlib.util.spec_from_file_location("max_postiz_pg_test_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(root, *, environment=None, engine_factory=None):
    env = os.environ if environment is None else environment
    contract = contract_module()
    specs = {role: contract.endpoint_spec(role, env.get(key), root, environment=env)
             for role, key in contract.URL_ENVS.items()}
    if engine_factory is None:
        from sqlalchemy import create_engine
        engine_factory = create_engine
    identities = []
    for role, key in contract.URL_ENVS.items():
        engine = engine_factory(env[key], connect_args={"connect_timeout": 5}, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                identities.append(contract.verify_identity(connection, specs[role]))
        finally:
            engine.dispose()
    if len(set(identities)) != 2:
        raise contract.PostgresTestContractError("PG_TEST_INSTANCES_NOT_DISTINCT")
    return {"contract": env[contract.CONTRACT_ENV], "postgres_major": 17, "instances": 2,
            "routes_port": specs["routes"]["port"], "monitor_port": specs["monitor"]["port"],
            "read_only": True, "identities_distinct": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, help="Require all isolated PG test cases from an existing pytest report")
    args = parser.parse_args(argv)
    try:
        if args.junit is not None:
            spec = importlib.util.spec_from_file_location("max_postiz_required_checks", ROOT / "scripts/check.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.owned_postgres_summary(args.junit)
        else:
            result = check(ROOT)
    except Exception:
        print("PG test preflight failed; check explicit contract and owned instances. Private details withheld.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
