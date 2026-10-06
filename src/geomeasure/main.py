from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from geomeasure.api import files, system
from geomeasure.config import Settings, get_settings
from geomeasure.db import create_db_engine, create_session_factory, init_db
from geomeasure.errors import InvalidUpload

DESCRIPTION = """
Upload a zipped Shapefile or a KML file and get per-feature measurements:
area and perimeter for polygons, length for lines, computed in a suitable projected CRS
and cross-checked against geodesic values on the WGS84 ellipsoid.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_db_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_db(engine)
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        yield
        engine.dispose()

    app = FastAPI(
        title="Geospatial File Measurement API",
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "files", "description": "Upload files and read their measurements."},
            {"name": "system", "description": "Operational endpoints."},
        ],
    )
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)

    app.include_router(files.router)
    app.include_router(system.router)

    @app.exception_handler(InvalidUpload)
    async def invalid_upload(_request: Request, exc: InvalidUpload) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app


app = create_app()
