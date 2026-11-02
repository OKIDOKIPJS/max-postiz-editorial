"""Наблюдатель в отдельной копии; MAX Bot API не используется."""

import argparse
import json
import ipaddress
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .cli import load_env
from .browser_runtime import profile_lock
from .config import AgentError
from .hub_cli import build
from .isolated_workspace import assert_isolated
from .monitor_config import MonitorConfig, bind_created_channel, initialize_monitor_config, private_path
from .monitor_service import MonitorService
from .routes_cli import initialize_copy
from .server_state import ServerState


def build_monitor(root):
    # До load_env/build проверяем принадлежность копии. Production DSN не открываем.
    assert_isolated(root, environment=False)
    load_env(root)
    assert_isolated(root)
    # Только явный init создаёт схему; обычные команды проверяют сохранённую.
    return build(root, activate=False, initialize=False)


def run(core, stop=None, *, control_only=False):
    """Долгая подготовка не задерживает отчёты и чтение управляющего чата."""
    stop = stop or threading.Event()
    signals = {name: threading.Event() for name in ("REPORTS", "RECEIVE", "TASKS")}

    def wait(name, seconds):
        deadline = time.monotonic() + seconds
        while not stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if signals[name].wait(min(remaining, 0.25)):
                signals[name].clear()
                return

    def lane(name, operation):
        while not stop.is_set():
            interval = 30
            try:
                config = MonitorConfig.load(core.settings.root)
                interval = config.value["poll_seconds"]
                result = operation(MonitorService(core, config))
                if isinstance(result, dict):
                    if name == "RECEIVE" and result.get("accepted", 0) > 0:
                        signals["TASKS"].set()
                        signals["REPORTS"].set()
                    elif name == "TASKS" and result.get("state") not in {
                            "idle", "disabled", "busy", "paused", "waiting_worker"}:
                        signals["REPORTS"].set()
                    elif name == "REPORTS" and result.get("state") == "confirmed":
                        # Outbox already owns the exact Send/unknown guards. Drain
                        # queued replies without another full polling interval.
                        interval = 0.25
            except AgentError as error:
                if error.code != "BROWSER_PROFILE_BUSY":
                    logging.getLogger(__name__).warning("MONITOR_%s_UNAVAILABLE", name)
            except Exception:
                # Ошибки браузера могут содержать приватный DOM/URL. В журнал идёт только код.
                logging.getLogger(__name__).warning("MONITOR_%s_UNAVAILABLE", name)
            wait(name, interval)

    def reports(service):
        if not control_only:
            service.produce()
        return service.deliver_once()

    threads = [threading.Thread(target=lane, args=(name, operation), daemon=True,
                                name="monitor-" + name.lower()) for name, operation in (
        ("REPORTS", reports), ("RECEIVE", lambda service: service.receive_once()),
        ("TASKS", lambda service: service.run_command_once()))]
    for thread in threads:
        thread.start()
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=10)


def watchdog_once(service, endpoint, instance, *, notify=False):
    import httpx
    from .monitor_watchdog import Watchdog, report_changes
    values = [os.getenv("MONITOR_WATCHDOG_" + key, "") for key in ("KEY", "OWNER", "ORGANIZATION")]
    headers = {}
    if any(values):
        if not all(values) or len(values[0]) < 32:
            raise AgentError("MONITOR_WATCHDOG_AUTH_INVALID")
        parsed = urlsplit(endpoint)
        if parsed.scheme == "http":
            try:
                local = ipaddress.ip_address(parsed.hostname).is_private
            except ValueError:
                local = parsed.hostname == "localhost"
            if not local:
                raise AgentError("MONITOR_WATCHDOG_HTTPS_REQUIRED")
        headers = dict(zip(("X-Editorial-Key", "X-Editorial-Owner", "X-Editorial-Organization"),
                           values, strict=True))
    with httpx.Client(headers=headers, trust_env=False) as client:
        result = Watchdog(client, endpoint, expected_instance=instance).check()
    if notify:
        if not service.config.value["enabled"]:
            raise AgentError("MONITOR_WATCHDOG_REPORTS_DISABLED")
        report_changes(service.store, result, service.queue)
        service.deliver_once()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Мониторинг MAX без официального бота")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("init")
    setup.add_argument("--source-root", type=Path, required=True)
    for name in ("status", "preview", "once", "deliver-once", "receive-once", "task-once", "run", "control-once", "control-run",
                 "login", "login-setup", "inspect", "inspect-control", "contract-template"):
        sub.add_parser(name)
    create = sub.add_parser("create-channel")
    create.add_argument("--recover-chat-id")
    for name in ("watchdog-once", "watchdog"):
        watcher = sub.add_parser(name)
        watcher.add_argument("--url", required=True)
        watcher.add_argument("--instance", required=True)
        watcher.add_argument("--notify", action="store_true")
        if name == "watchdog":
            watcher.add_argument("--interval", type=int, choices=range(15, 301), default=60)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("report_id")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    core = None
    try:
        if args.command == "init":
            if not (root / "config/app.json").exists():
                initialize_copy(root, args.source_root.resolve())
            else:
                # Не помечаем существующую установку как тестовую задним числом.
                assert_isolated(root)
            initialize_monitor_config(root)
            core = build(root)
            ServerState(core).initialize()
            from .article_cycle import HourlyArticleCycle
            HourlyArticleCycle(core)  # Только явный init создаёт пустой локальный каталог.
            result = {"state": "initialized_disabled", "production_access": False,
                      "external_mutations": 0, "bot_api": False}
        elif args.command == "contract-template":
            from .monitor_browser import control_contract_template
            result = {"template": control_contract_template(), "live_verified": False}
        else:
            core = build_monitor(root)
            config = MonitorConfig.load(root)
            service = MonitorService(core, config)
            if args.command == "status":
                result = service.status()
            elif args.command == "preview":
                from .monitor_reports import render_report
                print(render_report(service.snapshot(), report_id="preview", kind="hourly"))
                return 0
            elif args.command == "once":
                result = service.produce()
            elif args.command == "deliver-once":
                result = service.deliver_once()
            elif args.command == "receive-once":
                result = service.receive_once()
            elif args.command == "task-once":
                result = service.run_command_once()
            elif args.command == "control-once":
                result = {"receiver": service.receive_once(),
                          "task": service.run_command_once(), "reply": service.deliver_once()}
            elif args.command == "control-run":
                run(core, control_only=True)
                return 0
            elif args.command in {"watchdog-once", "watchdog"}:
                if args.command == "watchdog-once":
                    result = watchdog_once(service, args.url, args.instance, notify=args.notify)
                else:
                    stop = threading.Event()
                    try:
                        while not stop.is_set():
                            config = MonitorConfig.load(root)
                            service = MonitorService(core, config)
                            watchdog_once(service, args.url, args.instance, notify=args.notify)
                            stop.wait(args.interval)
                    except KeyboardInterrupt:
                        stop.set()
                    return 0
            elif args.command == "reconcile":
                result = service.reconcile(args.report_id)
            elif args.command in {"login", "login-setup", "inspect", "inspect-control", "create-channel"}:
                from .monitor_browser import MonitorBrowser
                options = config.browser_options()
                if args.command in {"login-setup", "create-channel"} and not options["report_chat_id"]:
                    options["report_chat_id"] = "0"  # Только setup, никогда не адресат отчёта.
                transport = MonitorBrowser(core.settings, options)
                with profile_lock(private_path(root, "data/monitor/transport.lock")):
                    if args.command == "create-channel":
                        if config.value["enabled"]:
                            raise AgentError("MONITOR_DISABLE_BEFORE_CHANNEL_SETUP")
                        result = transport.create_private_channel(
                            authorize=lambda name: config.current()
                                and name == config.value["report_chat_name"],
                            recover_chat_id=args.recover_chat_id)
                        if result.get("state") == "confirmed":
                            bind_created_channel(config, result)
                    else:
                        result = {"login": transport.login, "login-setup": transport.login_setup,
                                  "inspect": transport.inspect,
                                  "inspect-control": transport.inspect_control}[args.command]()
            else:
                run(core)
                return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except AgentError as exc:
        print(json.dumps({"code": exc.code, "production_access": False}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"code": "MONITOR_OPERATION_FAILED", "production_access": False}))
        return 2
    finally:
        if core is not None:
            core.close()


if __name__ == "__main__":
    raise SystemExit(main())
