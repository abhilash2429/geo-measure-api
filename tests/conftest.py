from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from geomeasure.config import Settings
from geomeasure.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
