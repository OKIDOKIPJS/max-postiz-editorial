#!/bin/sh
# Schema changes require a separate explicit owner operation.
set -eu
cd /app

cleanup() {
  trap - EXIT INT TERM
  # Fixed commands affect only this container's PM2 and nginx instances. Node
  # bounds both cleanup clients even if their local control socket is stuck.
  node -e '
    const { spawn } = require("node:child_process");
    const commands = [["pm2", ["kill"]], ["nginx", ["-s", "quit"]]];
    let pending = commands.length;
    const deadline = setTimeout(() => process.exit(1), 6000);
    for (const [command, args] of commands) {
      let child;
      try { child = spawn(command, args, { stdio: "ignore" }); }
      catch { if (--pending === 0) { clearTimeout(deadline); process.exit(1); } continue; }
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (--pending === 0) { clearTimeout(deadline); process.exit(0); }
      };
      const timer = setTimeout(() => { child.kill("SIGKILL"); finish(); }, 5000);
      child.once("error", finish);
      child.once("close", finish);
    }
  ' || :
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# Waiting on an explicitly owned background child lets the shell handle TERM
# while a startup client waits; the container owns all descendants at exit.
run() {
  "$@" &
  wait "$!"
}
run nginx
run pnpm --filter ./apps/backend run pm2
run pnpm --filter ./apps/orchestrator run pm2
run pnpm --filter ./apps/frontend run pm2
run pm2 logs
