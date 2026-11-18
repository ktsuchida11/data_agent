"""Langfuse の古い記録を消す（trace_retention。ADR-0031）。本物の Langfuse には接続しない。"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from data_agent.settings import CONFIG_DIR, SecurityConfig, _load_yaml
from data_agent.storage import trace_retention

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _trace(trace_id: str, days_ago: float, tags: list[str]) -> dict:
    return {"id": trace_id, "timestamp": NOW - timedelta(days=days_ago), "tags": tags}


def _select(traces: list[dict], tag: str, before: datetime) -> list[dict]:
    return sorted((t for t in traces if tag in t["tags"] and t["timestamp"] < before), key=lambda t: t["timestamp"])


class FakeTraces:
    """LangfuseTraces と同じ口を持つ、偽の Langfuse。"""

    def __init__(self, traces: list[dict]) -> None:
        self.traces = traces
        self.list_calls: list[dict] = []
        self.deleted: list[list[str]] = []

    async def list_ids(self, *, tag, before, page, limit):
        self.list_calls.append({"tag": tag, "before": before, "page": page})
        rows = _select(self.traces, tag, before)
        chunk = rows[(page - 1) * limit : page * limit]
        return [t["id"] for t in chunk], max(1, math.ceil(len(rows) / limit))

    async def delete(self, trace_ids):
        self.deleted.append(list(trace_ids))


@pytest.fixture
def audits(monkeypatch) -> list:
    events: list = []
    monkeypatch.setattr(trace_retention, "audit", lambda event, **fields: events.append((event, fields)))
    return events


def test_deletes_only_this_apps_traces_older_than_the_limit(audits) -> None:
    api = FakeTraces(
        [
            _trace("old-data", 91, ["data-agent", "claude-opus-5-5"]),
            _trace("old-coding", 120, ["coding-agent"]),
            _trace("new-data", 89, ["data-agent"]),
            _trace("old-other-app", 200, ["brew-app"]),  # ほかのアプリの記録は消さない
            _trace("old-both", 100, ["data-agent", "coding-agent"]),  # 2 つの印を持つものは、1 回だけ消す
        ]
    )
    assert asyncio.run(trace_retention.purge(api, 90, now=NOW)) == 3
    assert sorted(api.deleted[0]) == ["old-both", "old-coding", "old-data"]
    assert {call["tag"] for call in api.list_calls} == {"data-agent", "coding-agent"}
    assert all(call["before"] == NOW - timedelta(days=90) for call in api.list_calls)
    assert audits == [("trace_retention", {"days": 90, "deleted": 3})]


def test_collects_every_page_before_deleting_and_deletes_in_batches(audits) -> None:
    api = FakeTraces([_trace(f"t{i:04d}", 100 + i / 1000, ["data-agent"]) for i in range(2350)])
    assert asyncio.run(trace_retention.purge(api, 90, now=NOW)) == 2350
    assert [len(batch) for batch in api.deleted] == [1000, 1000, 350]  # 1 回に消せるのは 1000 件まで
    assert len({tid for batch in api.deleted for tid in batch}) == 2350
    assert [call["page"] for call in api.list_calls if call["tag"] == "data-agent"] == list(range(1, 25))


def test_stops_at_the_per_run_limit(audits, monkeypatch) -> None:
    monkeypatch.setattr(trace_retention, "MAX_PER_RUN", 250)
    api = FakeTraces([_trace(f"t{i}", 100, ["data-agent"]) for i in range(400)])
    assert asyncio.run(trace_retention.purge(api, 90, now=NOW)) == 250  # 残りは、次の回に消す


def test_count_only_does_not_delete(audits) -> None:
    api = FakeTraces([_trace("old", 91, ["data-agent"])])
    assert asyncio.run(trace_retention.purge(api, 90, now=NOW, delete=False)) == 1
    assert api.deleted == [] and audits == []


def test_zero_days_keeps_everything(audits) -> None:
    api = FakeTraces([_trace("old", 1000, ["data-agent"])])
    assert asyncio.run(trace_retention.purge(api, 0, now=NOW)) == 0
    assert api.list_calls == [] and api.deleted == []


def test_retention_days_setting() -> None:
    assert SecurityConfig().tracing.retention_days == 90
    assert SecurityConfig.model_validate(_load_yaml("security.yaml")).tracing.retention_days == 90
    assert (CONFIG_DIR / "security.yaml").read_text(encoding="utf-8").count("retention_days") == 1
    assert SecurityConfig.model_validate({"tracing": {"retention_days": 0}}).tracing.retention_days == 0
    with pytest.raises(ValidationError):
        SecurityConfig.model_validate({"tracing": {"retention_days": -1}})


def test_needs_the_server_address_as_well_as_the_keys(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):  # 送り先を書かずに、よそのサーバー（Langfuse の既定）へ向けない
        trace_retention.LangfuseTraces.from_env()


def test_loop_retries_after_a_failure_and_then_waits_a_day(monkeypatch, audits) -> None:
    waits: list[float] = []
    results = iter([RuntimeError("つながらない"), 2])

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 3:  # 失敗→やり直し→成功→1 日待つ、まで見たら止める
            raise asyncio.CancelledError

    async def fake_purge(days):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(trace_retention.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(trace_retention, "purge_with_env", fake_purge)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(trace_retention.retention_loop(90))
    assert waits == [
        trace_retention.FIRST_DELAY_SECONDS,
        trace_retention.RETRY_SECONDS,
        trace_retention.EVERY_SECONDS,
    ]
    assert audits == [("trace_retention", {"days": 90, "error": "RuntimeError"})]  # エラーの文は残さない


# ---------- 実際に送る形を確かめる（手元に立てた偽のサーバーに送る） ----------
class _FakeLangfuse(BaseHTTPRequestHandler):
    traces: list[dict] = []
    requests: list[dict] = []

    def log_message(self, *args) -> None:
        pass

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append({"method": "GET", "path": url.path, "query": query, "auth": self.headers["Authorization"]})
        if self.headers["Authorization"] != "Basic " + base64.b64encode(b"pk-lf-test:sk-lf-test").decode():
            self._reply(401, {"message": "Invalid credentials"})
            return
        rows = _select(self.traces, query["tags"], datetime.fromisoformat(query["toTimestamp"]))
        page, limit = int(query["page"]), int(query["limit"])
        # 本物と同じく、ほかの項目も返す（読むのは id だけ）
        data = [
            {
                "id": t["id"],
                "timestamp": t["timestamp"].isoformat(),
                "tags": t["tags"],
                "name": "LangGraph",
                "htmlPath": f"/project/p/traces/{t['id']}",
                "public": False,
                "environment": "default",
            }
            for t in rows[(page - 1) * limit : page * limit]
        ]
        meta = {"page": page, "limit": limit, "totalItems": len(rows), "totalPages": max(1, math.ceil(len(rows) / limit))}
        self._reply(200, {"data": data, "meta": meta})

    def do_DELETE(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append({"method": "DELETE", "path": self.path, "body": body})
        self._reply(200, {"message": "Traces deleted"})


@pytest.fixture
def fake_langfuse():
    _FakeLangfuse.traces = [_trace(f"old{i}", 91 + i, ["data-agent"]) for i in range(150)]
    _FakeLangfuse.traces += [_trace("new", 1, ["data-agent"]), _trace("other", 300, ["brew-app"])]
    _FakeLangfuse.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLangfuse)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _api(url: str, secret: str = "sk-lf-test") -> trace_retention.LangfuseTraces:
    # 送り先は http なので、証明書は使わない（作業用のサンドボックスは、証明書のファイルを読めない）
    return trace_retention.LangfuseTraces(url + "/", "pk-lf-test", secret, verify=False)


def test_sends_the_requests_langfuse_expects(fake_langfuse, audits) -> None:
    async def run() -> int:
        api = _api(fake_langfuse)
        try:
            return await trace_retention.purge(api, 90, now=NOW)
        finally:
            await api.aclose()

    assert asyncio.run(run()) == 150
    gets = [r for r in _FakeLangfuse.requests if r["method"] == "GET"]
    deletes = [r for r in _FakeLangfuse.requests if r["method"] == "DELETE"]
    assert {r["path"] for r in gets} == {"/api/public/traces"}
    first = gets[0]["query"]
    assert first["tags"] == "data-agent" and first["fields"] == "core" and first["orderBy"] == "timestamp.asc"
    assert first["toTimestamp"] == "2026-08-18T12:00:00.000Z"  # UTC の Z 付き（JavaScript の toISOString と同じ形）
    assert [r["query"]["page"] for r in gets if r["query"]["tags"] == "data-agent"] == ["1", "2"]
    assert len(deletes) == 1 and deletes[0]["path"] == "/api/public/traces"
    assert sorted(deletes[0]["body"]["traceIds"]) == sorted(f"old{i}" for i in range(150))


def test_wrong_keys_raise_and_delete_nothing(fake_langfuse, audits) -> None:
    import httpx

    async def run() -> None:
        api = _api(fake_langfuse, secret="sk-lf-wrong")
        try:
            await trace_retention.purge(api, 90, now=NOW)
        finally:
            await api.aclose()

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert not [r for r in _FakeLangfuse.requests if r["method"] == "DELETE"] and audits == []
