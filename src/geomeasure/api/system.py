from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from geomeasure.api.deps import SessionDep

router = APIRouter(tags=["system"])


@router.get("/health", summary="Liveness and database check")
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")
