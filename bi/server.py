"""FastAPI transport for the single-process synthetic BI demo."""
from __future__ import annotations

import asyncio
import json
import random
from contextlib import asynccontextmanager
from functools import wraps
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, Settings
from .contracts import AskRequest, QueryRequest
from .registry import Registry, build_registry
from .service import BIService

MAX_BODY = 64 * 1024
MAX_UPLOAD = 25 * 1024 * 1024
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def error(status, message):
    return JSONResponse({"ok": False, "error": message}, status_code=status)


class RequestBoundary:
    """Bound streamed bodies, active requests and body-read time; check same-origin access."""
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.slots = asyncio.Semaphore(settings.max_requests)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secured_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [(k.lower().encode(), v.encode()) for k, v in HEADERS.items()]
            await send(message)

        async def reject(status, message):
            await error(status, message)(scope, receive, secured_send)

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        host = headers.get("host", "")
        try:
            hostname = urlparse("//" + host).hostname
            origin = urlparse(headers["origin"]) if "origin" in headers else None
            if hostname not in self.settings.allowed_hosts:
                return await reject(403, "Host is not allowed.")
            if origin and (origin.netloc != host or origin.scheme != scope["scheme"]):
                return await reject(403, "Cross-origin requests are not allowed.")
            limit = MAX_UPLOAD if scope["path"] == "/api/upload" else MAX_BODY
            length = int(headers.get("content-length", "0"))
            if length < 0:
                return await reject(400, "Invalid content length.")
            if length > limit:
                return await reject(413, "Request body exceeds the size limit.")
        except ValueError:
            return await reject(400, "Invalid request headers.")
        if self.slots.locked():
            return await reject(503, "The demo is busy. Please retry shortly.")
        async with self.slots:
            async def read_body():
                chunks, total = [], 0
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        raise ConnectionError("Client disconnected")
                    chunk = message.get("body", b"")
                    total += len(chunk)
                    if total > limit:
                        raise OverflowError("Request too large")
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        return b"".join(chunks)
            try:
                body = await asyncio.wait_for(read_body(), timeout=self.settings.request_timeout)
            except OverflowError:
                return await reject(413, "Request body exceeds the size limit.")
            except asyncio.TimeoutError:
                return await reject(408, "Request body timed out.")
            except ConnectionError:
                return
            if scope["path"] in ("/api/ask", "/api/query"):
                try:
                    def invalid_constant(_):
                        raise ValueError("Non-finite JSON number")
                    json.loads(body, parse_constant=invalid_constant)
                except (ValueError, UnicodeError):
                    return await reject(400, "Invalid JSON body.")
            sent = False
            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()
            await self.app(scope, replay, secured_send)


def use_case(fn):
    """Run and close source connections on the same worker thread."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        request = kwargs["request"]
        reg = request.app.state.registry
        try:
            return fn(*args, **kwargs)
        except KeyError:
            return error(404, "Unknown data source or field.")
        except (ValueError, TypeError, AttributeError, OverflowError):
            return error(400, "Invalid request. Check the fields, values and synthetic-data confirmation.")
        except TimeoutError:
            return error(408, "Query timed out.")
        except Exception:
            return error(500, "Unable to read the source. Check its schema and availability.")
        finally:
            reg.close_connections()
    return wrapped


def create_app(settings: Optional[Settings] = None, registry: Optional[Registry] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if registry is not None and registry.settings != settings:
        raise ValueError("The registry and application must use the same settings.")

    @asynccontextmanager
    async def lifespan(app):
        app.state.registry = registry if registry is not None else build_registry(settings)
        app.state.service = BIService(app.state.registry)
        # Bootstrap may have opened a connection on this event-loop thread.
        app.state.registry.close_connections()
        try:
            yield
        finally:
            app.state.registry.shutdown()

    app = FastAPI(title="Conversational BI · synthetic demo", version="0.4.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.add_middleware(RequestBoundary, settings=settings)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo submitted questions, values or file contents in error details.
        return error(422, "Invalid request. Check the API contract and field limits.")

    @app.get("/api/health")
    @use_case
    def health(request: Request):
        return {"ok": True, "datasets": len(request.app.state.registry.engines), "data_classification": settings.data_classification}

    @app.get("/api/datasets")
    @use_case
    def datasets(request: Request):
        return {"datasets": request.app.state.registry.listing(), "data_classification": settings.data_classification}

    @app.get("/api/model")
    @use_case
    def model(request: Request, dataset: Optional[str] = None):
        return request.app.state.registry.get(dataset).catalog()

    @app.get("/api/samples")
    @use_case
    def samples(request: Request, dataset: Optional[str] = None):
        values = list(request.app.state.registry.get(dataset).model.sample_queries)
        random.shuffle(values)
        return {"samples": values}

    @app.get("/api/freshness")
    @use_case
    def freshness(request: Request, dataset: Optional[str] = None):
        return request.app.state.registry.get(dataset).freshness()

    @app.post("/api/ask")
    @use_case
    def ask(request: Request, body: AskRequest):
        return request.app.state.registry.ask(body.model_dump(exclude_none=True))

    @app.post("/api/query")
    @use_case
    def query(request: Request, body: QueryRequest):
        return request.app.state.service.query(body.model_dump(exclude_none=True))

    @app.post("/api/upload")
    async def upload(request: Request, x_filename: str = Header(default=""), x_data_classification: str = Header(default="")):
        from starlette.concurrency import run_in_threadpool
        payload = await request.body()
        def import_file():
            try:
                return request.app.state.service.upload(payload, x_filename, x_data_classification)
            except ValueError as exc:
                return error(400, str(exc))
            except Exception:
                return error(500, "Unable to import the synthetic file.")
            finally:
                request.app.state.registry.close_connections()
        return await run_in_threadpool(import_file)

    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
    return app


def main():
    import uvicorn
    settings = Settings.from_env()
    print(f"Synthetic-data demo on http://{settings.host}:{settings.port}", flush=True)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1,
                timeout_keep_alive=10, access_log=False)


if __name__ == "__main__":
    main()
