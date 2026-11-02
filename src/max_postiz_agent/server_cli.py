"""Запуск автономной редакции на VM без старой панели Hub."""
import argparse
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from .article_cycle import HourlyArticleCycle
from .config import AgentError
from .hub_cli import build
from .server_api import editorial_app, public_state
from .server_state import DatabaseArticleLedger, ServerState


def prepare_with_monitor(core, preparer):
    """Неисправный необязательный монитор не останавливает обычное пополнение."""
    chat_task = {"state": "idle"}
    if ((core.settings.root / "config/monitor.json").is_file()
            and core.db.engine.dialect.name == "postgresql"):
        from .monitor_config import MonitorConfig
        from .monitor_service import MonitorService
        try:
            monitor = MonitorService(core, MonitorConfig.load(core.settings.root))
            chat_task = monitor.run_command_once(worker_owned=True)
        except Exception:
            logging.getLogger(__name__).warning("MONITOR_COMMAND_CONFIG_UNAVAILABLE")
    if chat_task.get("state") in {"idle", "disabled", "paused"}:
        return preparer.prepare_once()
    return chat_task


def main(argv=None):
    parser = argparse.ArgumentParser(description="Автономная редакция Postiz")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Сохранить журнал в PostgreSQL; запуск остаётся на паузе")
    sub.add_parser("status")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8092)
    serve.add_argument("--api-only", action="store_true",
                       help="Проверка внутреннего API без второго обработчика")
    args = parser.parse_args(argv)
    # A restart/status must not activate configuration, synchronize every target,
    # revoke approvals or silently migrate the production database.
    core = build(args.root.resolve(), activate=False, initialize=args.command == "migrate")
    store = ServerState(core)
    try:
        if args.command == "migrate":
            cycle = HourlyArticleCycle(core)
            cycle._clear_outstanding(cycle._policy(enabled=False)["target_id"])
            result = DatabaseArticleLedger(cycle).migrate()
            store.initialize()
            print(json.dumps({"state": "migrated_paused", **result}, ensure_ascii=False))
        elif args.command == "status":
            print(json.dumps(public_state(core, store), ensure_ascii=False))
        else:
            import uvicorn
            from .hourly_reserve import PreparationState, ReserveServerCycle
            store.read()
            if DatabaseArticleLedger(HourlyArticleCycle(core)).load() is None:
                raise AgentError("EDITORIAL_MIGRATION_REQUIRED", 409)
            if args.api_only:
                uvicorn.run(editorial_app(core, store), host=args.host, port=args.port,
                            access_log=False, log_level="warning")
                return
            with store.worker_lock() as acquired:
                if not acquired:
                    raise AgentError("EDITORIAL_WORKER_ALREADY_RUNNING", 409)
                app = editorial_app(core, store)
                app.state.worker_mode = "autonomous"
                stop = threading.Event()
                # Разные экземпляры журнала: долгий prepare не владеет снимком отправителя.
                controller = ReserveServerCycle(core, state=store)
                preparer = ReserveServerCycle(core, state=PreparationState(store))

                def worker():
                    # Только этот поток выполняет внешние изменения Postiz/MAX.
                    while not stop.is_set():
                        try:
                            core.dispatch_tick()
                            # Отдельные согласования новых адресатов не наследуют
                            # MAX hourly policy; отправляет тот же владелец worker.
                            core.publish_due(platform_routes_only=True)
                            controller.run_once()
                        except Exception as exc:
                            code = exc.code if isinstance(exc, AgentError) else "EDITORIAL_WORKER_ERROR"
                            try:
                                store.phase("blocked", last_error={"code": code})
                            except Exception:
                                # Сбой БД не должен незаметно убить поток при живом HTTP-процессе.
                                logging.getLogger(__name__).error("EDITORIAL_STATE_UNAVAILABLE")
                        stop.wait(5)

                def prepare_worker():
                    while not stop.is_set():
                        try:
                            prepare_with_monitor(core, preparer)
                        except Exception as exc:
                            code = exc.code if isinstance(exc, AgentError) else "EDITORIAL_PREPARATION_ERROR"
                            try:
                                store.progress("preparation", "blocked", last_error={"code": code})
                            except Exception:
                                logging.getLogger(__name__).error("EDITORIAL_STATE_UNAVAILABLE")
                        stop.wait(5)

                @asynccontextmanager
                async def lifespan(application):
                    thread = threading.Thread(target=worker, name="editorial-cycle", daemon=True)
                    preparation = threading.Thread(target=prepare_worker, name="editorial-preparation", daemon=True)
                    thread.start()
                    preparation.start()
                    try:
                        yield
                    finally:
                        stop.set()
                        # Принудительный обрыв не вызывает повтор неизвестной операции.
                        thread.join(timeout=10)
                        preparation.join(timeout=10)

                app.router.lifespan_context = lifespan
                uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="warning")
    finally:
        core.close()


if __name__ == "__main__":
    main()
