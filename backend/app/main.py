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

from app.api.routes import (
    admin,
    auth,
    board,
    calendars,
    cleaners,
    console,
    disputes,
    health,
    payments,
    properties,
    reviews,
    turnovers,
)
from app.config import settings
from app.middleware import BodySizeLimit
from app.services import turnovers as turnover_rules
from app.services import places

API_PREFIX = "/api"

app = FastAPI(
    title="linx",
    description="A marketplace connecting STR property owners with independent cleaners.",
    version="0.1.0",
)

# **Outermost of the two, so the bytes are refused before anything reads them.**
# Starlette applies middleware in reverse, so adding this last puts it first:
# a body over the limit is answered without CORS, routing, or the multipart
# parser ever seeing it — which is the whole point, since `UploadFile` spools
# the body before the endpoint's auth dependency even runs.
app.add_middleware(BodySizeLimit, max_bytes=settings.max_request_bytes)

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
api_router.include_router(properties.router)
api_router.include_router(calendars.router)
api_router.include_router(turnovers.router)
api_router.include_router(cleaners.router)
api_router.include_router(board.router)
api_router.include_router(admin.router)
# The console sits beside the vetting queue under the same /admin prefix rather
# than absorbing it: the trust gate works and is tested, and phase 8 is not the
# moment to destabilise it for tidiness.
api_router.include_router(console.router)
api_router.include_router(payments.router)
api_router.include_router(reviews.router)
api_router.include_router(disputes.router)
app.include_router(api_router)


@app.get(f"{API_PREFIX}/config", tags=["meta"])
def public_config() -> dict[str, object]:
    """Settings the frontend is allowed to know.

    One region at launch — the frontend reads the name from here rather than
    hardcoding it, but there is deliberately no region *selection*.
    """
    return {
        "region_name": settings.region_name,
        "region_timezone": settings.region_timezone,
        # The Google Maps **browser** key, or null. This one is meant to be
        # public — the Maps JS SDK runs in the page, so the key is in the page
        # by definition, and Google's own model is that it is restricted by
        # HTTP referrer rather than kept secret.
        #
        # **That restriction is not optional.** An unrestricted Maps key found
        # in a page is somebody else's autocomplete on your bill. Restrict it
        # to this domain in the Google Cloud console, and to the Places and
        # Maps JavaScript APIs, before it is ever deployed.
        #
        # Null means the address form falls back to plain fields, which still
        # produce a placeable property — see `app/services/geocoding.py`.
        "google_maps_api_key": settings.google_maps_api_key,
        # **The bulk limit, so the form does not carry its own copy.** A screen
        # that lets somebody build a list the API will refuse is a form that
        # lies, and a second constant is how the two drift apart.
        "max_bulk_jobs": turnover_rules.MAX_BULK_JOBS,
    }


@app.get(f"{API_PREFIX}/places", tags=["meta"])
def public_places() -> list[dict[str, object]]:
    """The towns this product serves, with their centres.

    Public and unauthenticated because a cleaner picks their service area
    before they have an account, and because the list of towns in a region is
    not a secret.

    **The frontend reads this rather than carrying its own copy.** Two tables
    of coordinates that have to agree are two tables that eventually do not,
    and the one that is wrong is the one nobody is looking at.
    """
    return [
        {
            "name": place.name,
            "lat": place.lat,
            "lng": place.lng,
            "zips": list(place.zips),
        }
        for place in places.PLACES
    ]


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
