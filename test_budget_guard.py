"""Offline tests. Every HTTP request goes only to httpx.MockTransport."""
import asyncio
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import httpx

from budget_guard import (BudgetGuardError, BudgetTransport, COUNT_FIELDS, COUNT_URL,
                          RESPONSES_URL, conservative_cost)


def payload(**overrides):
    return {"model": "gpt-6-astra", "store": False, "service_tier": "default",
            "input": [{"role": "system", "content": "Use the tool."},
                      {"role": "user", "content": "Do the task."}],
            "tools": [{"type": "function", "name": "bash", "parameters": {
                "type": "object", "properties": {"command": {"type": "string"}},
                "required": ["command"]}}],
            "max_output_tokens": 100, **overrides}


def completed(inp=1000, out=20, **overrides):
    return {"status": "completed", "service_tier": "default", "usage": {
        "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
        "input_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 100},
        "output_tokens_details": {"reasoning_tokens": 10}}, **overrides}


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.seen = []

    async def make(self, count=1000, result=None, budget="20", handler=None):
        async def mock(request):
            self.seen.append(request)
            if handler:
                return await handler(request)
            if str(request.url) == COUNT_URL:
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": count})
            return httpx.Response(200, json=result or completed(count))
        self.guard = BudgetTransport(budget, self.directory, httpx.MockTransport(mock))
        self.guard.set_episode(1)
        self.client = httpx.AsyncClient(transport=self.guard)
        self.addAsyncCleanup(self.client.aclose)
        return self.guard

    async def post(self, p=None, url=RESPONSES_URL):
        return await self.client.post(url, json=p if p is not None else payload(),
                                      headers={"Authorization": "Bearer unit-test-secret"})

    async def test_complete_count_payload_and_subcounts(self):
        g = await self.make()
        p = payload(instructions="system", reasoning={"effort": "high"},
                    text={"format": {"type": "text"}}, tool_choice="auto",
                    parallel_tool_calls=True, truncation="disabled", include=["reasoning.encrypted_content"])
        response = await self.post(p)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.seen[0].content), {k: v for k, v in p.items() if k in COUNT_FIELDS})
        a = g.ledger["attempts"][0]
        self.assertEqual(Decimal(a["reservation_usd"]), Decimal("0.0175"))
        self.assertEqual(Decimal(a["estimated_cost_usd"]), Decimal("0.0135"))
        self.assertEqual(a["usage"]["output_tokens"], 20)
        self.assertEqual(g.ledger["paid_attempts"], 1)
        self.assertEqual(json.loads((self.directory / a["request_file"]).read_bytes()), p)
        contents = b"".join(f.read_bytes() for f in self.directory.rglob("*") if f.is_file())
        self.assertNotIn(b"unit-test-secret", contents)
        previous = "0" * 64
        for line in (self.directory / "budget_events.jsonl").read_bytes().splitlines():
            event = json.loads(line)
            digest = event.pop("sha256")
            self.assertEqual(event["previous_sha256"], previous)
            canonical = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), digest)
            previous = digest

    async def test_cache_write_reservation_blocks_before_paid_attempt(self):
        g = await self.make(count=100_000, budget="1.1")
        with self.assertRaises(BudgetGuardError):
            await self.post()
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(g.ledger["paid_attempts"], 0)
        self.assertEqual(g.stop_reason, "budget_would_be_exceeded")

    async def test_omitted_max_and_long_context_price_entire_request(self):
        g = await self.make(count=272_001)
        p = payload()
        del p["max_output_tokens"]
        await self.post(p)
        self.assertEqual(Decimal(g.ledger["attempts"][0]["reservation_usd"]), Decimal("16.400025"))
        self.assertEqual(conservative_cost(272_000, 128_000), Decimal("9.8"))

    async def test_unresolved_exception_retains_reservation_and_blocks_retry(self):
        async def handler(request):
            if str(request.url) == COUNT_URL:
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 1000})
            raise httpx.ReadTimeout("unsafe error unit-test-secret", request=request)
        g = await self.make(handler=handler)
        with self.assertRaises(BudgetGuardError) as raised:
            await self.post()
        self.assertNotIn("unit-test-secret", str(raised.exception))
        self.assertEqual(g.ledger["unresolved_reserved_usd"], "0.0175")
        with self.assertRaises(BudgetGuardError):
            await self.post()
        self.assertEqual(len(self.seen), 2)

    async def test_http_failure_preserves_sanitized_body_and_latches(self):
        async def handler(request):
            if str(request.url) == COUNT_URL:
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 1000})
            return httpx.Response(429, content=b'{"error":"unit-test-secret"}')
        g = await self.make(handler=handler)
        response = await self.post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(g.ledger["attempts"][0]["status"], "unresolved")
        saved = self.directory / g.ledger["attempts"][0]["response_file"]
        self.assertEqual(saved.read_bytes(), b'{"error":"[REDACTED_API_KEY]"}')

    async def test_count_failure_is_not_paid_attempt_and_no_retry(self):
        async def handler(request):
            return httpx.Response(500, json={"error": "temporary"})
        g = await self.make(handler=handler)
        with self.assertRaises(BudgetGuardError):
            await self.post()
        with self.assertRaises(BudgetGuardError):
            await self.post()
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(g.ledger["paid_attempts"], 0)

    async def test_rejected_endpoint_model_state_tools_and_truncation(self):
        cases = [(payload(), "https://api.openai.com/v1/models"),
                 (payload(model="gpt-6-astra-pro"), RESPONSES_URL),
                 (payload(access_programs=[]), RESPONSES_URL),
                 (payload(previous_response_id="resp_hidden"), RESPONSES_URL),
                 (payload(truncation="auto"), RESPONSES_URL),
                 (payload(tools=[{"type": "web_search"}]), RESPONSES_URL),
                 (payload(service_tier="priority"), RESPONSES_URL)]
        for n, (p, url) in enumerate(cases):
            with self.subTest(case=n):
                self.directory = Path(self.temp.name) / str(n)
                g = await self.make()
                with self.assertRaises(BudgetGuardError):
                    await self.post(p, url)
                self.assertEqual(g.ledger["paid_attempts"], 0)
        self.assertEqual(self.seen, [])

    async def test_usage_exceeds_count_latches_and_retains_upper_estimate(self):
        g = await self.make(result=completed(inp=1100))
        await self.post()
        self.assertEqual(g.stop_reason, "usage_exceeded_reservation")
        self.assertEqual(g.ledger["attempts"][0]["status"], "unresolved")

    async def test_output_limit_and_noncompletion_stop(self):
        for n, result in enumerate([completed(out=100), completed(status="incomplete")]):
            self.directory = Path(self.temp.name) / str(n)
            g = await self.make(result=result)
            await self.post()
            self.assertEqual(g.stop_reason, "response_not_completed_or_output_limit")

    async def test_requests_serialized_and_budget_shared(self):
        active = 0
        peak = 0
        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            if str(request.url) == COUNT_URL:
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 1000})
            return httpx.Response(200, json=completed())
        g = await self.make(handler=handler, budget="0.025")
        results = await asyncio.gather(self.post(), self.post(payload(metadata={"tag": "2"})), return_exceptions=True)
        self.assertEqual(peak, 1)
        self.assertEqual(g.ledger["paid_attempts"], 1)
        self.assertIsInstance(results[1], BudgetGuardError)


if __name__ == "__main__":
    unittest.main()
