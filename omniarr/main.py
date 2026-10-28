"""Starlette application: API, static UI, auth gate and background services."""
from __future__ import annotations

import asyncio
import contextlib

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import api, config, db, log, net, scheduler, search
from .indexers import catalog, defaults

L = log.get("main")

OPEN_PATHS = ("/api/auth/", "/static/", "/favicon", "/health")


class AuthGate(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and not path.startswith(OPEN_PATHS) and not api.session_ok(request):
            return JSONResponse({"error": "Please log in", "code": "auth"}, status_code=401)
        resp: Response = await call_next(request)
        if path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp


async def index(request: Request):
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request: Request):
    return JSONResponse({"ok": True, "version": config.VERSION})


@contextlib.asynccontextmanager
async def lifespan(app):
    log.setup()
    db.connect()
    L.info("Omniarr %s starting (config: %s)", config.VERSION, config.CONFIG_DIR)
    search.jobs.start()
    try:
        defaults.seed_on_first_start()
    except Exception:
        L.exception("Couldn't add the default public indexers")
    asyncio.create_task(catalog.summaries())   # warm the site list
    if config.SCHEDULER_ENABLED:
        scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await net.close_all()
        db.close()


def create_app() -> Starlette:
    routes = [
        Route("/", index),
        Route("/health", health),
        *api.routes,
        Mount("/static", app=StaticFiles(directory=str(config.STATIC_DIR)), name="static"),
    ]
    return Starlette(routes=routes, lifespan=lifespan,
                     middleware=[Middleware(GZipMiddleware, minimum_size=1000), Middleware(AuthGate)])


app = create_app()


def run() -> None:
    import uvicorn
    uvicorn.run("omniarr.main:app", host=config.BIND, port=config.PORT, log_level="warning",
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    run()
