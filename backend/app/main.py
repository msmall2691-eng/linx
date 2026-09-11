"""FastAPI application.

In production this single process serves both the JSON API (under `/api`) and
the built React frontend (everything else), so the whole product ships as one
container.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import auth, health
from app.config import settings

API_PREFIX = "/api"

app = FastAPI(
    title="linx",
    description="A marketplace connecting STR property owners with independent cleaners.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_router = APIRouter(prefix=API_PREFIX)
api_router.include_router(health.router)
api_router.include_router(auth.router)
app.include_router(api_router)


@app.get(f"{API_PREFIX}/config", tags=["meta"])
def public_config() -> dict[str, str]:
    """Settings the frontend is allowed to know.

    One region at launch — the frontend reads the name from here rather than
    hardcoding it, but there is deliberately no region *selection*.
    """
    return {"region_name": settings.region_name}


def _mount_frontend(application: FastAPI) -> None:
    """Serve the built frontend, if it has been built.

    Absent in development (Vite's dev server handles it) and in tests, so this
    is conditional rather than required.
    """
    dist = Path(__file__).resolve().parent.parent / settings.frontend_dist
    index = dist / "index.html"
    if not index.is_file():
        return

    assets = dist / "assets"
    if assets.is_dir():
        application.mount("/assets", StaticFiles(directory=assets), name="assets")

    @application.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        """Return index.html for client-side routes.

        Anything under the API prefix that reached here is a genuinely unknown
        endpoint, and must 404 as JSON rather than quietly returning an HTML
        page that a fetch() would then fail to parse.
        """
        if full_path.startswith(API_PREFIX.lstrip("/")):
            raise HTTPException(status_code=404, detail="Not found")

        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)


_mount_frontend(app)
