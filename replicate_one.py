"""One budget-guarded replication of the unchanged Goodhart public environment.

No key is accepted on the command line or placed in the sandbox. No automatic
second episode, retry, repository publication, or external communication.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import getpass
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE / "beat-stockfish"
COMMIT = "2fe51b6239a6dca70abfd70aca528ff4a0b3c3bf"
MESSAGE_LIMIT = 200
TIME_LIMIT = 10800


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as file:
        json.dump(value, file, indent=2, ensure_ascii=False)
        file.write("\n")


def command(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode:
        raise RuntimeError(f"{args[0]} preflight failed: {result.stderr[-1500:]}")
    return result.stdout.strip()


def source_record() -> dict:
    git = ("git", "-c", f"safe.directory={REPO.as_posix()}", "-C", str(REPO))
    if command(*git, "rev-parse", "HEAD") != COMMIT:
        raise RuntimeError("Repository is not at the frozen public commit")
    if command(*git, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Tracked public repository source changed")
    files = command(*git, "ls-files").splitlines()
    hashes = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in files}
    adapter_files = ["replicate_one.py", "astra_model.py", "budget_guard.py", "evidence.py"]
    adapters = {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in adapter_files}
    return {
        "repository_commit": COMMIT,
        "tracked_files_sha256": hashes,
        "adapter_files_sha256": adapters,
        "executed_source_sha256": hashlib.sha256(json.dumps({"upstream": hashes, "adapter": adapters}, sort_keys=True).encode()).hexdigest(),
    }


def load_rollout():
    spec = importlib.util.spec_from_file_location("goodhart_pinned_rollout", REPO / "run" / "rollout.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def configuration(budget: Decimal) -> dict:
    return {
        "attribution": "Replication of Goodhart Labs beat-stockfish; environment designed by Goodhart Labs",
        "api_model": "gpt-6-astra", "api": "OpenAI Responses", "service_tier": "default",
        "access_programs": "omitted; standard safeguards",
        "reasoning_effort": "omitted; model default", "reasoning_summary": "auto",
        "max_output_tokens": "omitted; guard reserves full documented 128000 maximum",
        "message_limit": MESSAGE_LIMIT, "agent_time_limit_seconds": TIME_LIMIT,
        "command_timeout_seconds": 600, "tool_output_cap_bytes_per_stream": 200000,
        "parallel_tool_calls": "unchanged provider default",
        "episodes": 1, "variant": "v001", "automatic_retries_all_layers": 0,
        "budget_ceiling_usd": str(budget),
        "budget_accounting": "full counted request input at highest cache-write price plus maximum output reserved before each paid request; unresolved failures retain reservation",
        "input_price_per_million_usd": "12.50 (conservative cache-write upper bound)",
        "output_price_per_million_usd": "50.00",
        "long_context_pricing": "above 272000 input: 25.00 input and 75.00 output per million for entire request",
        "temperature_top_p": "omitted; model defaults", "store": False,
        "history": "full stateless tool-call and encrypted reasoning history",
        "environment": "original Linux amd64 image; network none; original unprivileged model user",
        "stop": "after this one episode, or earlier infrastructure/budget failure; no automatic continuation",
    }


def make_task(rollout, image: str, episode: Path, secret: str):
    from inspect_ai.scorer import Score, mean, scorer
    from inspect_ai.solver import solver
    from inspect_ai.util import sandbox
    from evidence import GRADE_PATH, STATE_PATH, capture_sandbox_file, save_messages, validate_evidence

    task = rollout.environment(image=image, variants=["v001"], sandbox_command=None,
        message_limit_n=MESSAGE_LIMIT, time_limit_s=TIME_LIMIT, log_dir=episode / "inspect")
    original_solver = task.solver

    @solver
    def preserve_failure():
        async def solve(state, generate):
            try:
                return await original_solver(state, generate)
            except Exception as exc:
                # Retain an infrastructure/API failure and still try the original
                # grader on the actions already taken. This is never a retry.
                error = {"type": type(exc).__name__, "message": str(exc).replace(secret, "[REDACTED]")}
                save(episode / "execution-error.json", error)
                state.metadata["replication_execution_error"] = error
                return state
        return solve

    @scorer(metrics=[mean()])
    def capture_grader():
        async def score(state, target):
            save_messages(state, episode)
            metadata = {"captures": {}, "variant": "v001"}
            try:
                cleanup = await sandbox().exec(rollout._AGENT_CLEANUP, user="root")
                if not cleanup.success:
                    raise RuntimeError("Agent cleanup failed; grading cannot be trusted")
                metadata["captures"]["before"] = await capture_sandbox_file(sandbox(), STATE_PATH, episode / "state.before-grade.json")
                graded = await sandbox().exec(rollout.grade_argv("v001"), user="root")
                save(episode / "grader-process.json", {"success": graded.success, "returncode": graded.returncode,
                    "stdout": graded.stdout, "stderr": graded.stderr})
                metadata["captures"]["after"] = await capture_sandbox_file(sandbox(), STATE_PATH, episode / "state.json")
                metadata["captures"]["grade"] = await capture_sandbox_file(sandbox(), GRADE_PATH, episode / "grade.json")
                if not graded.success or not all(c.get("ok") for c in metadata["captures"].values()):
                    raise RuntimeError("Grader execution or evidence capture failed")
                for name in ("before", "after"):
                    if int(metadata["captures"][name]["mode"], 8) & 0o077:
                        raise RuntimeError("Private state permissions differ from the public environment")
                verified = validate_evidence(episode)
                save(episode / "independent-verdict.json", verified)
                if not verified["valid"]:
                    raise RuntimeError("Saved evidence disagrees with the frozen grader")
                grade = verified["grade"]
                return Score(value=float(grade["score"]), explanation=json.dumps(grade), metadata=grade)
            finally:
                save(episode / "capture.json", metadata)
        return score

    task.solver = preserve_failure()
    task.scorer = [capture_grader()]
    return task


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-usd", type=Decimal, required=True)
    parser.add_argument("--check", action="store_true", help="Check imports and frozen source; never connect to model API")
    args = parser.parse_args()
    if not args.budget_usd.is_finite() or args.budget_usd <= 0:
        raise ValueError("A positive separately approved dollar ceiling is required")

    import httpx
    from inspect_ai import eval as inspect_eval
    from astra_model import build_model
    from budget_guard import BudgetTransport
    from evidence import validate_evidence
    frozen = source_record()
    spec = configuration(args.budget_usd)
    versions = {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()}
    if versions.get("inspect_ai", versions.get("inspect-ai")) != "0.3.260":
        raise RuntimeError("Inspect version differs from public runner pin")
    if args.check:
        print(json.dumps({"configuration": spec, "source": frozen["executed_source_sha256"], "checks": "imports and frozen source passed; no Docker or API calls"}, indent=2))
        return 0

    build_files = sorted(HERE.glob("build-*.json"), reverse=True)
    build = next((json.loads(p.read_text(encoding="utf-8-sig")) for p in build_files
        if json.loads(p.read_text(encoding="utf-8-sig")).get("status") == "BUILD_COMPLETE"), None)
    if not build or build["checkout_commit"] != COMMIT:
        raise RuntimeError("Matching successful build record is missing")
    if command("docker", "info", "--format", "{{.OSType}}") != "linux":
        raise RuntimeError("A running Linux Docker engine is required")
    if command("docker", "image", "inspect", "beat-stockfish:local", "--format", "{{.Id}}") != build["image_id"]:
        raise RuntimeError("Docker image differs from the preserved build")

    lock_path = HERE / ".replication-active.lock"
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError:
        raise RuntimeError("Replication lock exists: inspect the current run before launching another") from None
    run = HERE / "runs" / ("astra-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    lock.write(json.dumps({"pid": os.getpid(), "run": str(run)})); lock.flush()
    key = ""
    client = None
    try:
        save(run / "specification.json", spec)
        save(run / "source.json", frozen)
        save(run / "dependencies.json", versions)
        save(run / "build.json", build)
        print(f"ONE episode; shared request spending ceiling USD {args.budget_usd}. Evidence: {run}", flush=True)
        if not sys.stdin.isatty():
            raise RuntimeError("Run this command in an interactive PowerShell window for masked key entry")
        key = getpass.getpass("OpenAI API key (masked; process memory only): ")
        if not key.strip():
            raise RuntimeError("No API key supplied")
        for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2"):
            logging.getLogger(name).setLevel(logging.WARNING)
        guard = BudgetTransport(args.budget_usd, run)
        guard.set_episode(run)
        client = httpx.AsyncClient(transport=guard, timeout=600.0, trust_env=False)
        model = build_model(key, client)
        rollout = load_rollout()
        task = make_task(rollout, build["image_id"], run, key)
        logs = inspect_eval(task, model=model, epochs=1, max_samples=1,
            log_dir=str(run / "inspect"), display="plain", retry_on_error=0,
            fail_on_error=True, log_model_api=True, log_realtime=True)
        verdict = validate_evidence(run)
        error_path = run / "execution-error.json"
        execution_error = json.loads(error_path.read_text(encoding="utf-8")) if error_path.exists() else None
        summary = {"run": str(run), "episodes_attempted": 1,
            "inspect_status": [log.status for log in logs], "verdict": verdict,
            "execution_error": execution_error,
            "budget": guard.ledger, "stop_reason": guard.stop_reason,
            "disclosure": "pending human review; consult messages.json and raw API outputs",
            "source_unchanged_after_run": source_record() == frozen}
        save(run / "RESULT.json", summary)
        print(json.dumps(summary, indent=2), flush=True)
        success = (verdict["valid"] and summary["source_unchanged_after_run"]
            and execution_error is None and guard.stop_reason is None
            and len(logs) == 1 and logs[0].status == "success")
        return 0 if success else 1
    except BaseException as exc:
        save(run / "RUN_STOPPED.json", {"type": type(exc).__name__, "message": str(exc).replace(key, "[REDACTED]") if key else str(exc)})
        print(f"Stopped ({type(exc).__name__}). Preserved evidence: {run}", flush=True)
        return 1
    finally:
        try:
            if client is not None and not client.is_closed:
                asyncio.run(client.aclose())
        except Exception as exc:
            save(run / "client-cleanup-error.json", {"type": type(exc).__name__})
        finally:
            key = ""
            lock.close()
            lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
