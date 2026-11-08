"""Synthetic-profile boundaries and framework integration regressions."""
import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from bi.config import Settings
from bi.registry import Registry, build_registry
from bi.server import MAX_BODY, RequestBoundary, create_app


class ProfileChecks(unittest.TestCase):
    def test_defaults_and_explicit_profile(self):
        settings = Settings()
        self.assertEqual(settings.data_classification, "synthetic")
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.data_dir.name, "synthetic")
        with self.assertRaises(ValueError):
            replace(settings, data_classification="real")
        with self.assertRaises(ValueError):
            replace(settings, source="postgresql://localhost/demo")
        self.assertEqual(replace(settings, source="mock.csv", source_classification="synthetic").source, "mock.csv")

    def test_network_and_configuration_are_explicit(self):
        with self.assertRaises(ValueError):
            Settings(host="0.0.0.0")
        self.assertTrue(Settings(host="0.0.0.0", allow_network=True).allow_network)
        for kwargs in [{"port": 0}, {"live_interval": 0}, {"max_requests": 0}, {"live_interval": float("nan")}, {"request_timeout": float("inf")}]:
            with self.assertRaises(ValueError):
                Settings(**kwargs)
        with patch.dict(os.environ, {"BI_DEMO": "maybe"}):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_factory_defers_io_until_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "not-created"
            create_app(Settings(data_dir=path, demo=False, live=False))
            self.assertFalse(path.exists())

    def test_unmarked_persisted_sources_are_not_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(data_dir=Path(folder), demo=False, live=False)
            uploads = settings.data_dir / "uploads"
            uploads.mkdir()
            source = uploads / "unknown.csv"
            source.write_text("Team,Revenue\nA,12\n")
            reg = build_registry(settings)
            self.assertEqual(reg.listing(), [])
            reg.shutdown()
            source.with_name(source.name + ".classification.json").write_text('{"classification":"synthetic"}')
            reg = build_registry(settings)
            try:
                self.assertEqual(len(reg.listing()), 1)
            finally:
                reg.shutdown()

    def test_live_feeder_stops_on_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(data_dir=Path(folder), live_interval=60)
            with TestClient(create_app(settings)) as client:
                feeder = client.app.state.registry.feeders[0]
                self.assertTrue(feeder.is_alive())
            self.assertFalse(feeder.is_alive())


class APIChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name), demo=False, live=False)
        self.reg = Registry(self.settings)
        self.reg.log_path = None
        self.client = TestClient(create_app(self.settings, self.reg))
        self.client.__enter__()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.client.__exit__, None, None, None)

    def upload(self, name="mock.csv", body=b"Date,Team,Revenue\n2026-09-01,A,12\n", **headers):
        return self.client.post("/api/upload", content=body, headers={"X-Filename": name, **headers})

    def test_upload_requires_attestation_then_survives_restart(self):
        self.assertEqual(self.upload().status_code, 400)
        self.assertFalse((self.settings.data_dir / "uploads").exists())
        response = self.upload(**{"X-Data-Classification": "synthetic"})
        self.assertEqual(response.status_code, 200, response.text)
        reg = build_registry(self.settings)
        try:
            self.assertIn(response.json()["dataset"], reg.engines)
        finally:
            reg.shutdown()

    def test_health_empty_sources_and_same_origin_headers(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.json()["data_classification"], "synthetic")
        self.assertEqual(self.client.get("/api/datasets").json()["datasets"], [])
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/health", headers={"Origin": "http://testserver"}).status_code, 200)
        self.assertEqual(self.client.get("/api/health", headers={"Origin": "https://testserver"}).status_code, 403)

    def test_openapi_has_nested_query_contract_and_no_remote_docs(self):
        response = self.client.get("/api/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertIn("AskRequest", schema["components"]["schemas"])
        self.assertIn("QueryInput", schema["components"]["schemas"])
        self.assertEqual(self.client.get("/docs").status_code, 404)

    def test_typed_requests_do_not_echo_sensitive_inputs(self):
        for body in [{"question": "secret", "extra": True}, {"question": "secret", "context": {"limit": True}},
                     {"question": "secret", "context": {"filters": [{"dimension": "team", "values": [True]}]}},
                     {"question": "secret", "context": {"time_range": {"start": "2026-10-02", "end": "2026-10-01"}}}]:
            with self.subTest(body=body):
                response = self.client.post("/api/ask", json=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("secret", response.text)
        self.assertEqual(self.client.post("/api/ask", content=b'{"question":"revenue","context":{"limit":NaN}}').status_code, 400)

    def test_query_and_followup_keep_contract_compatible(self):
        source = self.upload(**{"X-Data-Classification": "synthetic"}).json()["dataset"]
        result = self.client.post("/api/query", json={"dataset": source, "query": {"metrics": ["revenue"]}}).json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["totals"]["revenue"], 12)
        self.assertTrue(result["sql"])
        response = self.client.post("/api/ask", json={"dataset": source, "question": "now by team",
            "context_dataset": source, "context": result["query"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["query"]["dimensions"], ["team"])

    def test_application_instances_have_isolated_registries(self):
        self.upload(**{"X-Data-Classification": "synthetic"})
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(self.settings, data_dir=Path(folder))
            with TestClient(create_app(settings)) as other:
                self.assertEqual(other.get("/api/datasets").json()["datasets"], [])
        self.assertEqual(len(self.client.get("/api/datasets").json()["datasets"]), 1)


class StreamChecks(unittest.IsolatedAsyncioTestCase):
    async def call_boundary(self, chunks, settings=None):
        messages = []
        async def endpoint(scope, receive, send):
            raise AssertionError("Rejected request must not reach application")
        async def receive():
            return chunks.pop(0)
        async def send(message):
            messages.append(message)
        boundary = RequestBoundary(endpoint, settings or Settings())
        scope = {"type": "http", "path": "/api/ask", "scheme": "http", "headers": [(b"host", b"testserver")]}
        await boundary(scope, receive, send)
        return messages

    async def test_chunked_body_obeys_limit_without_content_length(self):
        result = await self.call_boundary([
            {"type": "http.request", "body": b"x" * MAX_BODY, "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False}])
        self.assertEqual(result[0]["status"], 413)

    async def test_stalled_body_has_deadline(self):
        async def endpoint(scope, receive, send):
            raise AssertionError("Should not reach application")
        async def stalled():
            await asyncio.Event().wait()
        messages = []
        async def send(message):
            messages.append(message)
        boundary = RequestBoundary(endpoint, replace(Settings(), request_timeout=.01))
        await boundary({"type": "http", "path": "/api/ask", "scheme": "http", "headers": [(b"host", b"testserver")]}, stalled, send)
        self.assertEqual(messages[0]["status"], 408)

    async def test_active_request_limit_rejects_overflow(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def endpoint(scope, receive, send):
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        first_messages, overflow_messages = [], []
        async def first_send(message):
            first_messages.append(message)
        async def overflow_send(message):
            overflow_messages.append(message)
        boundary = RequestBoundary(endpoint, replace(Settings(), max_requests=1))
        scope = {"type": "http", "path": "/", "scheme": "http", "headers": [(b"host", b"testserver")]}
        task = asyncio.create_task(boundary(scope, receive, first_send))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await boundary(scope, receive, overflow_send)
            self.assertEqual(overflow_messages[0]["status"], 503)
        finally:
            release.set()
            await task
        self.assertEqual(first_messages[0]["status"], 200)
