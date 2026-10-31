"""Приёмка и отправка согласованных записей только из отдельной тестовой копии."""

import argparse
import getpass
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from .cli import load_env
from .config import AgentError
from .db import now
from .hub_cli import build, init
from .hub_models import BridgePost, Dispatch, Target, Variant
from .isolated_workspace import assert_isolated, mark_isolated
from .platform_management import evidence, inspect_target, preflight, reconcile, route_state


def store_token(core, target_id, value):
    """Секрет вводится локально; смена не включает transport и всегда ставит паузу."""
    if (not isinstance(value, str) or not 20 <= len(value) <= 4096
            or not re.fullmatch(r"[A-Za-z0-9._:-]+", value)):
        raise AgentError("CREDENTIAL_FORMAT_INVALID")
    with core.db.session.begin() as session:
        target = session.scalar(select(Target).where(Target.id == target_id).with_for_update())
        if target is None or target.route not in {"telegram_api", "vk_api"}:
            raise AgentError("CREDENTIAL_TARGET_INVALID")
        if session.scalar(select(Dispatch.id).join(Variant).where(
                Variant.target_id == target.id, Dispatch.state.in_({"sending", "unknown"}))):
            raise AgentError("CANCEL_OR_RECONCILE_FIRST", 409)
        if session.scalar(select(BridgePost).where(BridgePost.target_id == target.id,
                                                   BridgePost.mutation_owner.is_not(None))):
            raise AgentError("BRIDGE_EDIT_IN_PROGRESS", 409)
        default = "TELEGRAM_BOT_TOKEN" if target.route == "telegram_api" else "VK_ACCESS_TOKEN"
        name = target.options.get("token_env", default)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name):
            raise AgentError("CREDENTIAL_ENV_INVALID")
        target.paused = True
        core.db.audit(session, "owner_local", "route_credential_change_paused", target.id,
                      credential_env=name, value_recorded=False)
    path = core.settings.root / ".env.local"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0].strip() != name]
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write("\n".join(lines + [name + "=" + value]) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return {"target_id": target_id, "credentials_configured": True, "paused": True,
            "restart_required": True, "external_mutations": 0}


def initialize_copy(root, source):
    mark_isolated(root, source)
    assert_isolated(root, environment=False)
    load_env(root)
    assert_isolated(root)
    if (root / "config/app.json").exists():
        raise AgentError("ISOLATED_COPY_ALREADY_INITIALIZED", 409)
    init(root, sqlite=True)
    path = root / "config/app.json"
    app = json.loads(path.read_text(encoding="utf-8"))
    app["api"]["port"] = 18092
    app["opencode"]["adapter_mode"] = "fake"
    app["max"]["adapter_mode"] = "fake"
    path.write_text(json.dumps(app, ensure_ascii=False, indent=2), encoding="utf-8")
    source_path = root / "config/sources.json"
    sources = json.loads(source_path.read_text(encoding="utf-8-sig"))
    for source_item in sources["sources"]:
        source_item["enabled"] = False
    source_path.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    publishing = json.loads((root / "config/four_platform.publishing.example.json").read_text(encoding="utf-8"))
    (root / "config/publishing.json").write_text(json.dumps(publishing, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"state": "initialized_paused", "root": str(root), "external_mutations": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description="MAX / Telegram / VK / Пикабу: тестовая копия")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("init")
    setup.add_argument("--source-root", type=Path, required=True)
    for name in ("status", "dispatch-once"):
        sub.add_parser(name)
    worker = sub.add_parser("worker")
    worker.add_argument("--interval", type=int, default=5, choices=range(2, 61))
    for name, field in (("inspect", "target_id"), ("login", "target_id"),
                        ("pause", "target_id"), ("resume", "target_id"),
                        ("preflight", "variant_id"), ("reconcile", "dispatch_id"),
                        ("evidence", "dispatch_id")):
        sub.add_parser(name).add_argument(field)
    sub.add_parser("variants").add_argument("draft_id")
    sub.add_parser("set-token").add_argument("target_id")
    original = sub.add_parser("create-original")
    original.add_argument("target_id")
    original.add_argument("--file", type=Path, required=True)
    original.add_argument("--attest-original", action="store_true",
                          help="Оператор подтверждает собственные права на точный текст и media; отправки нет")
    approval = sub.add_parser("approve")
    approval.add_argument("variant_id")
    approval.add_argument("--revision", type=int, required=True)
    approval.add_argument("--content-hash", required=True)
    approval.add_argument("--at", required=True, help="ISO дата с часовым поясом, например +03:00")
    args = parser.parse_args(argv)
    root, core = args.root.resolve(), None
    try:
        if args.command == "init":
            result = initialize_copy(root, args.source_root)
        else:
            assert_isolated(root, environment=False)
            load_env(root)
            assert_isolated(root)
            core = build(root)
            if args.command == "status":
                result = route_state(core)
            elif args.command == "preflight":
                result = preflight(core, args.variant_id)
            elif args.command == "variants":
                result = {"variant_ids": core.make_variants(args.draft_id)}
            elif args.command == "create-original":
                from .owner_original import create_original, read_original_file
                if not args.attest_original:
                    raise AgentError("OWNER_ORIGINAL_ATTESTATION_REQUIRED", 403)
                result = create_original(core, args.target_id, read_original_file(root, args.file),
                                         attest_original=args.attest_original)
            elif args.command == "set-token":
                value = getpass.getpass("Токен для копии (ввод скрыт): ")
                result = store_token(core, args.target_id, value)
            elif args.command == "inspect":
                result = inspect_target(core, args.target_id)
            elif args.command in {"pause", "resume"}:
                core.pause_target(args.target_id, args.command == "pause")
                result = {"target_id": args.target_id, "paused": args.command == "pause"}
            elif args.command == "login":
                with core.db.session() as session:
                    target = core._target(session, args.target_id)
                adapter = (core.routes.publisher(target) if target.route in {"pikabu_browser", "vk_browser"}
                           else core.publisher)
                if target.route not in {"pikabu_browser", "max_browser", "vk_browser"}:
                    raise AgentError("ROUTE_LOGIN_NOT_APPLICABLE")
                if not hasattr(adapter, "login"):
                    raise AgentError("BROWSER_MODE_NOT_ENABLED")
                result = adapter.login(target.destination)
            elif args.command == "approve":
                date = datetime.fromisoformat(args.at)
                if date.tzinfo is None:
                    raise AgentError("SCHEDULE_TIMEZONE_REQUIRED")
                if date.timestamp() <= now():
                    raise AgentError("SCHEDULE_FUTURE_REQUIRED", 422)
                ident = core.approve_variant(args.variant_id, args.revision,
                    args.content_hash, date.timestamp(), actor="owner")
                result = {"dispatch_id": ident, "state": "approved"}
            elif args.command == "reconcile":
                result = reconcile(core, args.dispatch_id)
            elif args.command == "evidence":
                result = {"evidence": evidence(core, args.dispatch_id)}
            elif args.command == "dispatch-once":
                result = {"published": core.publish_due()}
            else:
                stop = threading.Event()
                while not stop.wait(args.interval):
                    core.publish_due()
                result = {"state": "stopped"}
        print(json.dumps(result, ensure_ascii=False))
    except (AgentError, ValueError) as exc:
        print(json.dumps({"code": exc.code if isinstance(exc, AgentError) else "INPUT_INVALID"}))
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Внешний transport/DSN никогда не попадает в консоль как traceback.
        print(json.dumps({"code": "ISOLATED_ROUTE_OPERATION_FAILED"}))
        return 1
    finally:
        if core is not None:
            core.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
