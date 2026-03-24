"""
OmicsClaw Memory API Server.

FastAPI application serving the graph memory REST API.
Start with: python -m omicsclaw.memory.server
         or: oc memory-server

Requires: pip install fastapi uvicorn
"""

import os
import secrets
from contextlib import asynccontextmanager

# Guard: FastAPI is optional
try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


def _build_app():
    """Build and configure the FastAPI application."""
    if not _HAS_FASTAPI:
        return None

    @asynccontextmanager
    async def lifespan(app):
        from . import get_db_manager
        db = get_db_manager()
        await db.init_db()
        yield
        from . import close_db
        await close_db()

    app = FastAPI(
        title="OmicsClaw Memory API",
        description="Graph-based memory system for OmicsClaw multi-omics platform",
        version="1.0.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Optional bearer token auth
    _api_token = os.getenv("OMICSCLAW_MEMORY_API_TOKEN", "")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if not _api_token:
            return await call_next(request)
        if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc"):
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        provided = auth_header.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(provided, _api_token):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)

    # Register API routers
    from .api.browse import router as browse_router
    from .api.review import router as review_router
    from .api.maintenance import router as maintenance_router

    app.include_router(browse_router)
    app.include_router(review_router)
    app.include_router(maintenance_router)

    @app.get("/health", tags=["health"])
    async def health_check():
        from sqlalchemy import text
        from . import get_db_manager
        db_status = "disconnected"
        try:
            db = get_db_manager()
            async with db.session() as session:
                await session.execute(text("SELECT 1"))
            db_status = "connected"
        except Exception:
            pass
        status_code = 200 if db_status == "connected" else 503
        return JSONResponse(
            content={
                "status": "ok" if db_status == "connected" else "degraded",
                "database": db_status,
                "service": "omicsclaw-memory",
            },
            status_code=status_code,
        )

    return app


# Build app at module level (None if fastapi not installed)
app = _build_app()


def main():
    """Entry point for running the memory API server."""
    if not _HAS_FASTAPI:
        print("ERROR: FastAPI is not installed.")
        print("Install with: pip install fastapi uvicorn")
        raise SystemExit(1)

    import uvicorn

    host = os.getenv("OMICSCLAW_MEMORY_HOST", "0.0.0.0")
    port = int(os.getenv("OMICSCLAW_MEMORY_PORT", "8766"))

    print(f"OmicsClaw Memory API starting on http://{host}:{port}")
    print(f"API docs: http://{host}:{port}/docs")

    uvicorn.run(
        "omicsclaw.memory.server:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
