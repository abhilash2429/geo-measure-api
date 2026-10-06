from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GEOMEASURE_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/geomeasure.db"

    max_upload_mb: int = 50
    # Zip-bomb guards: total extracted size and the compression ratio we are willing to accept.
    max_extracted_mb: int = 500
    max_compression_ratio: int = 100
    max_zip_entries: int = 1000

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


@lru_cache
def get_settings() -> Settings:
    return Settings()
