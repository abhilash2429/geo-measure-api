FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    GEOMEASURE_DATA_DIR=/data \
    GEOMEASURE_DATABASE_URL=sqlite:////data/geomeasure.db
VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "geomeasure.main:app", "--host", "0.0.0.0", "--port", "8000"]
