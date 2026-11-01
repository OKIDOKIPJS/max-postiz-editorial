"""Pikabu browser transport with a dedicated profile and durable single-click worker."""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .adapters import Delivery
from .browser_process import run_browser_worker
from .config import AgentError, fingerprint, inside
from .pikabu_delivery import READBACK_CONTRACT, PikabuDelivery, owner_name, public_tag_contract
from .pikabu_metadata import story_url


class PikabuBrowser:
    mode = "browser"

    def __init__(self, root, options=None):
        self.root = Path(root).resolve()
        self.options = options or {}
        self.runtime_generation = None
        self.preparation = PikabuDelivery(self.root, self.options)

    def validate(self, destination, snapshot):
        plan = self.preparation.validate(destination, snapshot)
        expected = owner_name(self.options.get("owner"))
        if destination not in {expected, "user:" + expected, "https://pikabu.ru/@" + expected}:
            raise AgentError("PIKABU_DESTINATION_OWNER_MISMATCH")
        if plan["community"]:
            raise AgentError("PIKABU_BROWSER_COMMUNITY_UNSUPPORTED")
        from .pikabu_browser_worker import _body_contract, _dom, _media_contract
        _dom(self.options, publish=True)
        _media_contract(self.options, plan)
        _body_contract(plan)
        return plan

    def _call(self, action, destination, **payload):
        request = {"action": action, "destination": destination,
                   "options_sha256": fingerprint(self.options), **payload}
        if action in {"publish", "prepare"}:
            if (not isinstance(self.runtime_generation, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", self.runtime_generation)):
                raise AgentError("PIKABU_RUNTIME_RESTART_REQUIRED", 409)
            request["runtime_generation"] = self.runtime_generation
        try:
            result = run_browser_worker(
                [sys.executable, "-m", "max_postiz_agent.pikabu_browser_worker",
                 "--root", str(self.root)],
                input=json.dumps(request),
                timeout=min(360, int(self.options.get("operation_timeout_seconds", 180))) + 60)
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise AgentError("PIKABU_BROWSER_OPERATION_UNKNOWN", 503) from exc
        if "code" in value:
            from .pikabu_browser_worker import _safe_failure_code
            raise AgentError(_safe_failure_code(value["code"]), 409)
        return value

    def inspect(self, destination):
        return self._call("inspect", destination)

    def probe(self, destination):
        """Only selectors/labels/counts; never fills or submits the live editor."""
        return self._call("probe", destination)

    def login(self, destination):
        owner_name(self.options.get("owner"))
        directory = inside(self.root, "data/pikabu-login")
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "stdout.log").open("w", encoding="utf-8") as out, \
                (directory / "stderr.log").open("w", encoding="utf-8") as err:
            process = subprocess.Popen(
                [sys.executable, "-m", "max_postiz_agent.pikabu_browser_worker",
                 "--root", str(self.root), "--login", destination],
                stdout=out, stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return {"state": "login_window_requested", "pid": process.pid,
                "live_verified": False, "profile": self.options.get("profile_dir", "data/pikabu-browser")}

    def _bound_journal(self, destination, snapshot, request_id):
        from .pikabu_browser_worker import ORIGIN, _dom, _read_journal
        _, journal = _read_journal(self.root, request_id)
        if (journal.get("fixture") is not False or journal.get("execution_origin") != ORIGIN
                or journal.get("destination") != destination or journal.get("owner") != self.options.get("owner")
                or journal.get("options_sha256") != fingerprint(self.options)
                or journal.get("dom_contract_sha256") != fingerprint(_dom(self.options, publish=True))
                or journal.get("snapshot_sha256") != fingerprint(snapshot)
                or fingerprint(journal.get("snapshot")) != fingerprint(snapshot)):
            raise AgentError("PIKABU_RECEIPT_INVALID")
        return journal

    def _delivery(self, value, destination, snapshot, request_id, *, fresh_after=None):
        """Child stdout alone cannot establish delivery, even when verified=true."""
        from .pikabu_browser_worker import _result
        try:
            plan = self.validate(destination, snapshot)
            journal = self._bound_journal(destination, snapshot, request_id)
            if (not isinstance(value, dict) or fingerprint(value) != fingerprint(_result(journal))
                    or type(journal.get("clicks")) is not int or not 0 <= journal["clicks"] <= 1):
                raise ValueError
            if value["state"] == "confirmed":
                proof = journal.get("proof")
                if not isinstance(proof, dict) or journal["clicks"] != 1:
                    raise ValueError
                permalink = story_url(value.get("permalink"))
                ident = value.get("external_id")
                if (not isinstance(ident, str) or not re.fullmatch(r"[0-9]{1,20}", ident)
                        or value.get("permalink") != permalink or journal.get("readback_url") != permalink
                        or not permalink.endswith("_" + ident) or proof.get("permalink") != permalink
                        or proof.get("external_id") != ident):
                    raise ValueError
                expected = {"contract": READBACK_CONTRACT, "owner": self.options["owner"],
                    "snapshot_sha256": fingerprint(snapshot),
                    "title_sha256": hashlib.sha256(plan["title"].encode("utf-8")).hexdigest(),
                    "body_sha256": hashlib.sha256(plan["body"].encode("utf-8")).hexdigest(),
                    "tags_sha256": fingerprint(sorted(tag.strip().casefold() for tag in plan["tags"])),
                    "source_urls_sha256": fingerprint([source.get("url") for source in snapshot.get("sources", [])]),
                    "image_sha256": [item["sha256"] for item in plan["media"]],
                    "verification": "independent_reload_exact_text_owner_links_tags_image_sha256"}
                expected.update(public_tag_contract(snapshot, proof.get("platform_added_tags")))
                if any(proof.get(key) != expected_value for key, expected_value in expected.items()):
                    raise ValueError
                for key in ("html_sha256", "public_dom_sha256"):
                    if not isinstance(proof.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", proof[key]):
                        raise ValueError
                urls = proof.get("image_urls")
                if (not isinstance(urls, list) or len(urls) != len(plan["media"])
                        or any(not isinstance(url, str) or not re.fullmatch(
                            r"https://cs\d*\.pikabu\.ru/[^?#]+", url) for url in urls)):
                    raise ValueError
                observed = proof.get("observed_at")
                if (type(observed) not in {int, float} or not 0 < observed <= 1e12 or not math.isfinite(observed)
                        or observed > time.time() + 5 or observed < journal.get("created_at", math.inf)
                        or (fresh_after is not None and observed < fresh_after)):
                    raise ValueError
            return Delivery(value["state"], value.get("external_id"), value.get("permalink"),
                            verified=value["verified"] is True, evidence=value["evidence"])
        except (AgentError, KeyError, ValueError, TypeError, OSError):
            return Delivery("unknown", verified=False, evidence={"kind": "browser_protocol_error",
                            "failure_code": "PIKABU_RECEIPT_INVALID"})

    def publish(self, destination, snapshot, request_id):
        self.validate(destination, snapshot)
        try:
            return self._delivery(self._call("publish", destination, snapshot=snapshot,
                                             request_id=request_id), destination, snapshot, request_id)
        except AgentError as exc:
            # Worker failure alone does not establish whether upload/click occurred.
            return self._worker_error(exc)

    @staticmethod
    def _worker_error(exc):
        from .pikabu_browser_worker import _safe_failure_code
        return Delivery("unknown", verified=False, evidence={"kind": "browser_worker_error",
                        "failure_code": _safe_failure_code(exc.code)})

    def prepare(self, destination, snapshot, request_id):
        self.validate(destination, snapshot)
        return self._delivery(self._call("prepare", destination, snapshot=snapshot,
                                         request_id=request_id), destination, snapshot, request_id)

    def reconcile(self, destination, request_id, *, snapshot=None):
        try:
            self.validate(destination, snapshot)
            self._bound_journal(destination, snapshot, request_id)
            started = time.time()
            return self._delivery(self._call("reconcile", destination, request_id=request_id),
                                  destination, snapshot, request_id, fresh_after=started)
        except AgentError as exc:
            return self._worker_error(exc)

    def close(self):
        pass
