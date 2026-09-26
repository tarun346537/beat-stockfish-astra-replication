"""Fail-closed, serialized budget transport for this exact replication.

Use one instance for the whole run, with SDK/Inspect retries disabled. No headers
are persisted. Dollar values are decimal strings, and settled costs deliberately
charge every input token at the highest cache-write rate (an estimate, not a bill).

Count schema verified 2026-09-26 against official OpenAI API reference:
https://developers.openai.com/api/reference/typescript/resources/responses/subresources/input_tokens/methods/count
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

MODEL = "gpt-6-astra"
RESPONSES_URL = "https://api.openai.com/v1/responses"
COUNT_URL = RESPONSES_URL + "/input_tokens"
MAX_OUTPUT_TOKENS = 128_000
LONG_INPUT_THRESHOLD = 272_000
COUNT_FIELDS = frozenset({
    "model", "input", "instructions", "parallel_tool_calls", "personality",
    "reasoning", "text", "tool_choice", "tools", "truncation",
})


class BudgetGuardError(RuntimeError):
    """A local block; messages contain no request headers or network exceptions."""


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def conservative_cost(input_tokens: int, output_tokens: int) -> Decimal:
    """All-input cache-write upper estimate; subcounts are never added twice."""
    input_rate, output_rate = ((Decimal("25"), Decimal("75"))
                              if input_tokens > LONG_INPUT_THRESHOLD
                              else (Decimal("12.50"), Decimal("50")))
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


class BudgetTransport(httpx.AsyncBaseTransport):
    def __init__(self, budget_dollars: str | Decimal | int, run_dir: str | Path,
                 underlying: httpx.AsyncBaseTransport | None = None) -> None:
        self.budget = Decimal(str(budget_dollars))
        if not self.budget.is_finite() or self.budget <= 0:
            raise ValueError("Budget must be a finite positive dollar amount")
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._ledger_path = self.run_dir / "budget_ledger.json"
        self._event_path = self.run_dir / "budget_events.jsonl"
        # Refuse accidental restart or a second independent budget for this run.
        self._owner_path = self.run_dir / ".budget_guard_owner"
        self._owner_fd = os.open(self._owner_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        if self._ledger_path.exists() or self._event_path.exists():
            os.close(self._owner_fd)
            self._owner_path.unlink()
            raise BudgetGuardError("Existing ledger requires review; automatic resume forbidden")
        self._underlying = underlying or httpx.AsyncHTTPTransport(retries=0)
        self._lock = asyncio.Lock()
        self._episode: str | None = None
        self._episode_dir: Path | None = None
        self._history: dict[str, list[Any]] = {}
        self._seen: set[tuple[str, str]] = set()
        self._closed = False
        self._event_hash = "0" * 64
        self._ledger: dict[str, Any] = {
            "schema_version": 1, "model": MODEL, "budget_usd": str(self.budget),
            "cost_label": "conservative_estimated_not_provider_bill",
            "settled_estimated_usd": "0", "unresolved_reserved_usd": "0",
            "committed_usd": "0", "remaining_usd": str(self.budget),
            "paid_attempts": 0, "count_calls": 0, "attempts": [],
            "stop_reason": None, "last_event_sha256": self._event_hash,
        }
        self._event("initialized")

    @property
    def ledger(self) -> dict[str, Any]:
        return copy.deepcopy(self._ledger)

    @property
    def stop_reason(self) -> str | None:
        return self._ledger["stop_reason"]

    def set_episode(self, directory_or_index: str | Path | int) -> None:
        if self._lock.locked():
            raise BudgetGuardError("Cannot change episode during a request")
        if isinstance(directory_or_index, int):
            directory = self.run_dir / f"episode-{directory_or_index:03d}"
        else:
            directory = Path(directory_or_index)
            if not directory.is_absolute():
                directory = self.run_dir / directory
        directory = directory.resolve()
        if not directory.is_relative_to(self.run_dir):
            raise ValueError("Episode evidence directory must be inside run_dir")
        directory.mkdir(parents=True, exist_ok=True)
        self._episode = str(directory.relative_to(self.run_dir))
        self._episode_dir = directory

    def _event(self, kind: str, **fields: Any) -> None:
        settled = sum((Decimal(a.get("estimated_cost_usd", "0"))
                       for a in self._ledger["attempts"] if a["status"] == "settled"), Decimal(0))
        reserved = sum((Decimal(a["reservation_usd"])
                        for a in self._ledger["attempts"] if a["status"] != "settled"), Decimal(0))
        self._ledger.update(settled_estimated_usd=str(settled),
                            unresolved_reserved_usd=str(reserved),
                            committed_usd=str(settled + reserved),
                            remaining_usd=str(self.budget - settled - reserved))
        event = {"time_utc": datetime.now(timezone.utc).isoformat(), "event": kind,
                 "previous_sha256": self._event_hash, **fields,
                 "committed_usd": self._ledger["committed_usd"],
                 "stop_reason": self.stop_reason}
        digest = hashlib.sha256(_json(event)).hexdigest()
        event["sha256"] = digest
        with self._event_path.open("ab") as f:
            f.write(_json(event) + b"\n")
            f.flush()
            os.fsync(f.fileno())
        self._event_hash = digest
        self._ledger["last_event_sha256"] = digest
        temp = self._ledger_path.with_suffix(".json.tmp")
        with temp.open("wb") as f:
            f.write(_json(self._ledger) + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self._ledger_path)

    def _stop(self, reason: str, **fields: Any) -> None:
        if self.stop_reason is None:
            self._ledger["stop_reason"] = reason
        self._event("stopped", reason=reason, **fields)

    def _validate(self, request: httpx.Request, p: Any) -> None:
        if request.method != "POST" or str(request.url) != RESPONSES_URL:
            raise BudgetGuardError("Only the exact Responses POST endpoint is allowed")
        if not isinstance(p, dict) or p.get("model") != MODEL:
            raise BudgetGuardError("Only the exact requested model is allowed")
        if p.get("store") is not False:
            raise BudgetGuardError("store must explicitly be false")
        if p.get("stream", False) is not False or p.get("background", False) is not False:
            raise BudgetGuardError("Streaming and background responses are forbidden")
        if p.get("service_tier") not in (None, "default"):
            raise BudgetGuardError("Only Standard/default service tier is allowed")
        if p.get("truncation", "disabled") != "disabled":
            raise BudgetGuardError("Input truncation is forbidden")
        if any(k in p for k in ("previous_response_id", "conversation", "access_programs",
                                "prompt", "context_management")):
            raise BudgetGuardError("Stored state, access programs, or context compaction forbidden")
        inputs = p.get("input")
        if not isinstance(inputs, list) or not inputs:
            raise BudgetGuardError("Full explicit input history is required")
        allowed_items = {"message", "reasoning", "function_call", "function_call_output"}
        if any(not isinstance(i, dict) or i.get("type", "message") not in allowed_items for i in inputs):
            raise BudgetGuardError("Only inline messages, reasoning, and function history allowed")
        if not any(i.get("role") == "user" for i in inputs):
            raise BudgetGuardError("Full input history must include its user message")
        previous = self._history.get(self._episode or "")
        if previous is not None and inputs[:len(previous)] != previous:
            raise BudgetGuardError("Earlier explicit input history was dropped or changed")
        tools = p.get("tools")
        if not isinstance(tools, list) or not tools or any(
            not isinstance(t, dict) or t.get("type") != "function" for t in tools
        ):
            raise BudgetGuardError("Explicit function tool schemas are required; hosted tools forbidden")
        choice = p.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") not in ("function", "allowed_tools"):
            raise BudgetGuardError("Hosted tool selection forbidden")
        if isinstance(choice, dict) and choice.get("type") == "allowed_tools" and any(
            not isinstance(t, dict) or t.get("type") != "function" for t in choice.get("tools", [])
        ):
            raise BudgetGuardError("Hosted tool selection forbidden")
        maximum = p.get("max_output_tokens", MAX_OUTPUT_TOKENS)
        if not _integer(maximum) or not 0 < maximum <= MAX_OUTPUT_TOKENS:
            raise BudgetGuardError("Unsupported max_output_tokens")

    @staticmethod
    def _secret(request: httpx.Request) -> bytes:
        # Inspect only the known authorization header; never serialize headers.
        authorization = request.headers.get("authorization", "")
        return authorization.removeprefix("Bearer ").encode("utf-8")

    def _save(self, directory: Path, name: str, body: bytes, secret: bytes) -> str:
        # Preserve bytes exactly except the actual key if an API error echoes it.
        safe = body.replace(secret, b"[REDACTED_API_KEY]") if secret else body
        path = directory / name
        with path.open("xb") as f:
            f.write(safe)
            f.flush()
            os.fsync(f.fileno())
        return str(path.relative_to(self.run_dir))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async with self._lock:
            if self._closed or self.stop_reason:
                raise BudgetGuardError("Budget guard stopped: " + (self.stop_reason or "closed"))
            if self._episode_dir is None:
                raise BudgetGuardError("set_episode must precede requests")
            raw = await request.aread()
            try:
                payload = json.loads(raw)
                self._validate(request, payload)
            except (ValueError, BudgetGuardError) as e:
                reason = str(e) if isinstance(e, BudgetGuardError) else "Invalid request JSON"
                self._stop("request_rejected", detail=reason)
                raise BudgetGuardError(reason) from None
            digest = hashlib.sha256(raw).hexdigest()
            request_identity = (self._episode or "", digest)
            if request_identity in self._seen:
                self._stop("duplicate_payload")
                raise BudgetGuardError("Duplicate request payload; automatic retry forbidden")
            evidence = self._episode_dir / ("request-" + uuid4().hex)
            evidence.mkdir()
            secret = self._secret(request)
            request_file = self._save(evidence, "request.json", raw, secret)
            count_payload = {k: v for k, v in payload.items() if k in COUNT_FIELDS}
            count_raw = _json(count_payload)
            self._save(evidence, "input_tokens_request.json", count_raw, secret)
            count_headers = dict(request.headers)
            count_headers.pop("content-length", None)
            count_headers.pop("idempotency-key", None)
            count_request = httpx.Request("POST", COUNT_URL, headers=count_headers,
                                          content=count_raw, extensions=request.extensions)
            self._ledger["count_calls"] += 1
            self._event("count_started", request_sha256=digest, episode=self._episode)
            try:
                count_response = await self._underlying.handle_async_request(count_request)
                count_body = await count_response.aread()
                self._save(evidence, "input_tokens_response.json", count_body, secret)
                await count_response.aclose()
                if count_response.status_code != 200:
                    raise BudgetGuardError("Count endpoint rejected request")
                count_data = json.loads(count_body)
                input_tokens = count_data.get("input_tokens")
                if not _integer(input_tokens) or count_data.get("object") != "response.input_tokens":
                    raise BudgetGuardError("Invalid token count response")
            except BaseException:
                self._stop("input_token_count_failed")
                raise BudgetGuardError("Input count failed; no paid request was dispatched") from None
            maximum = payload.get("max_output_tokens", MAX_OUTPUT_TOKENS)
            reservation = conservative_cost(input_tokens, maximum)
            if Decimal(self._ledger["committed_usd"]) + reservation > self.budget:
                self._stop("budget_would_be_exceeded", required_reservation_usd=str(reservation),
                           input_tokens=input_tokens, max_output_tokens=maximum)
                raise BudgetGuardError("Budget would be exceeded; paid request not dispatched")
            attempt = {"sequence": len(self._ledger["attempts"]) + 1,
                       "episode": self._episode, "status": "reserved",
                       "request_sha256": digest, "request_file": request_file,
                       "counted_input_tokens": input_tokens,
                       "max_output_tokens_reserved": maximum,
                       "reservation_usd": str(reservation)}
            self._ledger["attempts"].append(attempt)
            self._ledger["paid_attempts"] += 1
            self._seen.add(request_identity)
            self._history[self._episode or ""] = copy.deepcopy(payload["input"])
            self._event("paid_request_reserved", attempt=copy.deepcopy(attempt))
            try:
                response = await self._underlying.handle_async_request(request)
                response_body = await response.aread()
                attempt["response_file"] = self._save(evidence, "response.json", response_body, secret)
                attempt["http_status"] = response.status_code
                if response.status_code != 200:
                    attempt["status"] = "unresolved"
                    self._stop("paid_http_error", sequence=attempt["sequence"], http_status=response.status_code)
                    return response
                data = json.loads(response_body)
                usage = data.get("usage")
                if not isinstance(usage, dict) or not all(
                    _integer(usage.get(k)) for k in ("input_tokens", "output_tokens", "total_tokens")
                ) or usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
                    raise BudgetGuardError("Missing or invalid usage")
                for detail_key, sub_key, total_key in (
                    ("input_tokens_details", "cached_tokens", "input_tokens"),
                    ("input_tokens_details", "cache_write_tokens", "input_tokens"),
                    ("output_tokens_details", "reasoning_tokens", "output_tokens"),
                ):
                    details = usage.get(detail_key) or {}
                    value = details.get(sub_key, 0)
                    if not _integer(value) or value > usage[total_key]:
                        raise BudgetGuardError("Invalid usage subcount")
                attempt["usage"] = usage
                attempt["response_status"] = data.get("status")
                estimate = conservative_cost(usage["input_tokens"], usage["output_tokens"])
                attempt["estimated_cost_usd"] = str(estimate)
                if usage["input_tokens"] > input_tokens or usage["output_tokens"] > maximum or estimate > reservation:
                    attempt["status"] = "unresolved"
                    # Retain at least the reservation if count/bounds proved unreliable.
                    attempt["reservation_usd"] = str(max(reservation, estimate))
                    self._stop("usage_exceeded_reservation", sequence=attempt["sequence"])
                else:
                    attempt["status"] = "settled"
                    self._event("paid_request_settled", attempt=copy.deepcopy(attempt))
                    if data.get("status") != "completed" or usage["output_tokens"] >= maximum:
                        self._stop("response_not_completed_or_output_limit", sequence=attempt["sequence"])
                    elif data.get("service_tier") not in (None, "default") or data.get("access_programs"):
                        self._stop("unexpected_response_billing_mode", sequence=attempt["sequence"])
                return response
            except BaseException:
                attempt["status"] = "unresolved"
                self._stop("paid_request_unresolved", sequence=attempt["sequence"])
                raise BudgetGuardError("Paid request unresolved; reservation retained and all future calls blocked") from None

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._underlying.aclose()
            os.close(self._owner_fd)
            self._owner_path.unlink(missing_ok=True)

    async def close(self) -> None:
        await self.aclose()
