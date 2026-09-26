"""User-run, API-free checkpoint for exactly the already authorized live episode.

Suspends the verified host while checking original root evidence. No event or
invalid live evidence resumes it. A verified engine-use event permits permanent
early stop, original agent cleanup, original grading, and preserved container.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
RUN = HERE / "runs" / "astra-20260926T210236-df927892"
EXPECTED_PID = 19224


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def norm(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(value))).replace("\\", "/")


def command(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def checked(argv: list[str]) -> str:
    result = command(argv)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} failed: {result.stderr[-1500:]}")
    return result.stdout


def find_container(image_id: str) -> dict:
    resolved = json.loads(checked(["docker", "image", "inspect", image_id]))
    allowed_images = {image_id, *(entry["Id"] for entry in resolved)}
    ids = checked(["docker", "ps", "-q"]).split()
    if not ids:
        raise RuntimeError("No running containers; host was not suspended")
    inspected = json.loads(checked(["docker", "inspect", *ids]))
    expected_config = norm(RUN / "inspect" / "compose.json")
    matches = []
    for item in inspected:
        labels = item["Config"].get("Labels") or {}
        configs = labels.get("com.docker.compose.project.config_files", "").split(",")
        if (expected_config in [norm(config) for config in configs]
                and labels.get("com.docker.compose.service") == "default"
                and item["State"]["Running"] and not item["State"].get("Paused")
                and item["HostConfig"]["NetworkMode"] == "none"
                and (item["Config"]["Image"] == image_id or item["Image"] in allowed_images)):
            matches.append({"id": item["Id"], "image": item["Image"],
                            "configured_image": item["Config"]["Image"],
                            "labels": labels, "created": item["Created"]})
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one matching container, found {len(matches)}")
    return matches[0]


class RootSandbox:
    def __init__(self, container_id: str):
        self.container_id = container_id

    async def exec(self, argv: list[str], user: str = "root"):
        if user != "root":
            raise ValueError("Checkpoint commands require root")
        result = command(["docker", "exec", "--user", "root", self.container_id, *argv], 45)
        return SimpleNamespace(success=result.returncode == 0, returncode=result.returncode,
                               stdout=result.stdout, stderr=result.stderr)


async def capture_grade(sandbox, rollout, target: Path) -> dict:
    from evidence import capture_sandbox_file, validate_evidence, STATE_PATH, GRADE_PATH
    target.mkdir()
    metadata = {}
    metadata["before"] = await capture_sandbox_file(sandbox, STATE_PATH, target / "state.before-grade.json")
    save(target / "before-capture.json", metadata["before"])
    before = metadata["before"]
    if not before.get("ok") or before.get("uid") != 0 or int(before.get("mode", "0"), 8) != 0o600:
        raise RuntimeError("Live private state is not successfully captured as root-owned mode 0600")
    graded = await sandbox.exec(rollout.grade_argv("v001"))
    save(target / "grader-process.json", vars(graded))
    metadata["after"] = await capture_sandbox_file(sandbox, STATE_PATH, target / "state.json")
    metadata["grade"] = await capture_sandbox_file(sandbox, GRADE_PATH, target / "grade.json")
    save(target / "captures.json", metadata)
    verdict = validate_evidence(target)
    verified_capture = (graded.success and all(row.get("ok") and row.get("uid") == 0
                                               for row in metadata.values())
                        and all(int(metadata[name].get("mode", "0"), 8) == 0o600
                                for name in ("before", "after")))
    verdict["capture_metadata_valid"] = verified_capture
    if not verified_capture:
        verdict["valid"] = False
        verdict["event"] = False
        verdict["engine_use"] = False
        verdict["errors"].append("Root ownership, private permissions, or grader process check failed")
    save(target / "independent-verdict.json", verdict)
    return verdict


async def main() -> int:
    import psutil
    from replicate_one import source_record, load_rollout
    from evidence import capture_sandbox_file

    if len(sys.argv) != 1:
        raise RuntimeError("This script takes no arguments and targets one fixed episode")
    lock_path = HERE / ".replication-active.lock"
    lock_raw = lock_path.read_bytes()
    lock = json.loads(lock_raw)
    if lock.get("pid") != EXPECTED_PID or norm(lock.get("run", "")) != norm(RUN):
        raise RuntimeError("Active lock does not identify the exact authorized episode")
    process = psutil.Process(EXPECTED_PID)
    process_birth = process.create_time()
    cmdline = process.cmdline()
    allowed_executables = {norm(HERE / ".venv" / "Scripts" / "python.exe"),
                           norm(Path(sys.base_prefix) / "python.exe")}
    workspace = HERE.parent.parent.resolve()
    process_cwd = Path(process.cwd()).resolve()
    script_argument = Path(cmdline[1]) if len(cmdline) > 1 else Path("missing")
    if not script_argument.is_absolute():
        script_argument = process_cwd / script_argument
    if (norm(process.exe()) not in allowed_executables or not process_cwd.is_relative_to(workspace)
            or len(cmdline) != 4 or norm(script_argument) != norm(HERE / "replicate_one.py")
            or cmdline[2:] != ["--budget-usd", "20"]):
        raise RuntimeError("Host executable, working directory, or exact command line does not match")
    frozen = json.loads((RUN / "source.json").read_text(encoding="utf-8"))
    if source_record() != frozen:
        raise RuntimeError("Frozen original source or existing adapter hashes changed")
    build = json.loads((RUN / "build.json").read_text(encoding="utf-8"))
    container = find_container(build["image_id"])
    rollout = load_rollout()
    target = RUN / ("live-checkpoint-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                    + "-" + uuid.uuid4().hex[:8])
    target.mkdir()
    save(target / "preflight.json", {"run": str(RUN), "pid": EXPECTED_PID,
        "process_created": process_birth, "cmdline": cmdline, "container": container,
        "source": frozen, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "lock": lock, "started_utc": datetime.now(timezone.utc).isoformat(),
        "paid_api_calls_by_checkpoint": 0})
    sandbox = RootSandbox(container["id"])
    suspended = False
    committed_stop = False
    host_stopped = False
    result = {"run": str(RUN), "checkpoint": str(target), "paid_api_calls_by_checkpoint": 0,
              "host_pid": EXPECTED_PID, "container_id": container["id"],
              "original_environment_and_grader_source_unchanged": True,
              "container_policy": "stop and preserve; never remove",
              "budget_policy": "preserve unresolved reservation; an in-flight request may still be charged"}
    try:
        if lock_path.read_bytes() != lock_raw or process.create_time() != process_birth:
            raise RuntimeError("Host identity changed before suspension")
        process.suspend()
        suspended = True
        for name in ("budget_ledger.json", "budget_events.jsonl"):
            with (target / ("at-suspension-" + name)).open("xb") as handle:
                handle.write((RUN / name).read_bytes())
        live_verdict = None
        for attempt in range(3):
            live_dir = target / f"live-verification-{attempt + 1}"
            live_verdict = await capture_grade(sandbox, rollout, live_dir)
            result["live_verification"] = str(live_dir)
            if live_verdict["valid"]:
                break
            if live_verdict["errors"] != ["state changed across grading; exact graded state is ambiguous"]:
                break
        if not live_verdict or not live_verdict["valid"] or not live_verdict["engine_use"]:
            result.update(status="RESUMED_NO_VERIFIED_ENGINE_USE", live_verdict=live_verdict)
            return_code = 2
        else:
            # A valid original grade and matching raw root state prove the event.
            # Kill the suspended host first: model cleanup alone could dispatch API work.
            if not process.is_running() or process.create_time() != process_birth:
                raise RuntimeError("Verified host identity changed before permanent stop")
            committed_stop = True
            process.kill()
            process.wait(timeout=15)
            host_stopped = True
            suspended = False
            cleanup = await sandbox.exec(rollout._AGENT_CLEANUP)
            save(target / "agent-cleanup.json", vars(cleanup))
            if not cleanup.success:
                raise RuntimeError("Original agent cleanup failed after verified event")
            canonical = target / "canonical"
            final_verdict = await capture_grade(sandbox, rollout, canonical)
            if not final_verdict["valid"] or not final_verdict["engine_use"]:
                raise RuntimeError("Canonical evidence failed verification after verified live event")
            result.update(status="VERIFIED_ENGINE_USE_EARLY_STOP", verdict=final_verdict,
                          canonical_evidence=str(canonical),
                          source_unchanged_after_stop=(source_record() == frozen))
            return_code = 0
    except BaseException as exc:
        result.update(status="INFRASTRUCTURE_FAILURE_AFTER_VERIFIED_EVENT" if committed_stop
                      else "RESUMED_CHECKPOINT_FAILURE", error={"type": type(exc).__name__, "message": str(exc)})
        return_code = 1
    finally:
        if committed_stop:
            if not host_stopped and process.is_running():
                try:
                    process.kill()
                    process.wait(timeout=15)
                    host_stopped = True
                    suspended = False
                except Exception as exc:
                    result["host_stop_error"] = {"type": type(exc).__name__, "message": str(exc)}
            try:
                stopped = command(["docker", "stop", "--time", "10", container["id"]], 30)
                save(target / "container-stop.json", {"returncode": stopped.returncode,
                     "stdout": stopped.stdout, "stderr": stopped.stderr})
                result["container_stopped"] = stopped.returncode == 0
            except Exception as exc:
                result["container_stop_error"] = {"type": type(exc).__name__, "message": str(exc)}
        elif suspended:
            try:
                process.resume()
                suspended = False
                result["host_resumed"] = True
            except Exception as exc:
                result["resume_error"] = {"type": type(exc).__name__, "message": str(exc)}
                result["status"] = "HOST_RESUME_FAILED_REQUIRES_ATTENTION"
        result["host_stopped"] = host_stopped
        result["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save(target / "EARLY_STOP_RESULT.json", result)
        print(json.dumps(result, indent=2))
        print(f"Preserved checkpoint: {target}", flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
