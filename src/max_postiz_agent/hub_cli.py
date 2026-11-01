"""Standalone hub CLI; runtime secrets and database URLs never enter output."""
import argparse
import json
import secrets
import shutil
import threading
from pathlib import Path

from .cli import initialize, load_env
from .config import AgentError, Settings
from .db import Database
from .hub import HubService


def init(root, sqlite=False):
    initialize(root)
    env = root / ".env.local"
    text = env.read_text(encoding="utf-8")
    for key in ("APP_AUTH_SECRET", "APP_EDITOR_SECRET", "APP_VIEWER_SECRET"):
        if not any(line.startswith(key + "=") for line in text.splitlines()):
            text += key + "=" + secrets.token_urlsafe(32) + "\n"
    env.write_text(text, encoding="utf-8")
    app_path = root / "config/app.json"
    app = json.loads(app_path.read_text(encoding="utf-8-sig"))
    app["require_postgresql"] = not sqlite
    app["api"]["port"] = 8090
    app["video"]["backend"] = "disabled"
    app_path.write_text(json.dumps(app, ensure_ascii=False, indent=2), encoding="utf-8")
    target = root / "config/publishing.json"
    if not target.exists():
        shutil.copy2(root / "config/publishing.example.json", target)


def build(root, activate=True, *, initialize=True):
    load_env(root)
    settings = Settings.load(root)
    db = Database(settings)
    if initialize:
        db.initialize()
    else:
        from .server_bootstrap import require_schema
        try:
            require_schema(db)
        except Exception:
            db.engine.dispose()
            raise
    service = HubService(settings, db, bootstrap_targets=activate)
    if activate:
        service.activate()
    return service


def main(argv=None):
    parser = argparse.ArgumentParser(description="Редакционная связка MAX / Пикабу / Postiz")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("init")
    command.add_argument("--sqlite", action="store_true", help="Explicit isolated development mode")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    serve.add_argument("--no-worker", action="store_true")
    serve.add_argument("--dispatch-only", action="store_true",
                       help="Only reconcile Postiz and send human-approved hourly MAX articles")
    sub.add_parser("doctor")
    sub.add_parser("tick")
    sub.add_parser("worker")
    history = sub.add_parser("import-history")
    history.add_argument("database", type=Path)
    history.add_argument("--apply", action="store_true")
    package = sub.add_parser("export-project")
    package.add_argument("destination", type=Path)
    backup = sub.add_parser("backup")
    backup.add_argument("destination", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("archive", type=Path)
    probe = sub.add_parser("probe-llm")
    probe.add_argument("--draft", action="store_true")
    collect = sub.add_parser("collect")
    collect.add_argument("source_id")
    stage = sub.add_parser("variants")
    stage.add_argument("draft_id")
    inspect = sub.add_parser("inspect-target")
    inspect.add_argument("target_id")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "init":
        init(root, args.sqlite)
        print("Initialized; external delivery remains paused")
        return
    service = None
    try:
        service = build(root, activate=args.command != "restore")
        if args.command == "serve":
            import uvicorn
            from .hub_api import hub_app
            uvicorn.run(hub_app(service, not args.no_worker, dispatch_only=args.dispatch_only),
                        host=args.host, port=args.port, log_level="warning", access_log=False)
        elif args.command == "worker":
            stop = threading.Event()
            while not stop.wait(5):
                service.tick()
        elif args.command == "doctor":
            from sqlalchemy import select
            from .hub_models import Target
            with service.db.session() as s:
                targets = [{"id": t.id, "platform": t.platform, "route": t.route,
                            "enabled": t.enabled, "paused": t.paused} for t in s.scalars(select(Target))]
            print(json.dumps({"database": service.db.engine.dialect.name,
                              "opencode": service.editor.probe(), "model": service.editor.model,
                              "postiz_configured": bool(service.postiz.base and service.postiz.key),
                              "targets": targets}, ensure_ascii=False))
        elif args.command == "probe-llm":
            result = service.probe_llm(args.draft)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "collect":
            print(json.dumps({"accepted": service.collect(args.source_id)}))
        elif args.command == "variants":
            print(json.dumps({"variant_ids": service.make_variants(args.draft_id)}))
        elif args.command == "inspect-target":
            from .hub_models import Target
            with service.db.session() as s:
                t = service._target(s, args.target_id)
            if t.route in {"max_browser", "max_api"}:
                print(json.dumps(service.publisher.inspect(t.destination), ensure_ascii=False))
            elif t.route == "manual":
                print(json.dumps({"state": "manual", "destination": t.destination, "live_verified": False}))
            else:
                print(json.dumps({"integrations": service.postiz.integrations()}))
        elif args.command == "import-history":
            from .history_import import import_history
            print(json.dumps(import_history(service.db, args.database, args.apply), ensure_ascii=False))
        elif args.command == "export-project":
            from .portability import package_project
            print(json.dumps(package_project(root, args.destination), ensure_ascii=False))
        elif args.command in {"backup", "restore"}:
            import os
            from .backup import backup, restore
            key = os.getenv("HUB_BACKUP_KEY", "")
            if not key:
                raise AgentError("BACKUP_KEY_REQUIRED")
            if args.command == "backup":
                args.destination.parent.mkdir(parents=True, exist_ok=True)
                backup(service, args.destination, key)
                print(json.dumps({"state": "backed_up", "encrypted": True}))
            else:
                restore(service, args.archive, key)
                print(json.dumps({"state": "restored", "delivery_paused": True}))
        else:
            print(json.dumps({"published": service.tick()}))
    except AgentError as exc:
        print(json.dumps({"code": exc.code}))
        raise SystemExit(1) from None
    finally:
        if service:
            service.close()


if __name__ == "__main__":
    main()
