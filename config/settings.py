"""Application configuration — environment-variable driven (12-factor).

Nothing business-critical is hardcoded here: reconciliation tolerance, the
RT/RTO decision strategy and the indicator column are all overridable via env.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Application
    app_name: str = "AR Reconciliation"
    app_env: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql+psycopg2://ar_user:ar_pass@localhost:5432/ar_recon"

    # Reconciliation business rules
    recon_tolerance: float = Field(default=1.00, ge=0)
    rt_rto_strategy: str = "indicator_then_presence"
    rt_rto_indicator_column: str = "return_type"
    # Opt-in: assert packet_id and order_release_id are the SAME identifier, so
    # the final stage can join directly without a link file. Off by default —
    # the tool never assumes this silently.
    assume_packet_equals_order: bool = False

    # Ingestion
    excel_chunk_rows: int = 50_000
    upload_dir: Path = Path("./data/uploads")
    cache_dir: Path = Path("./data/cache")
    max_upload_mb: int = 512

    # CORS — comma-separated origins allowed to call the API
    cors_origins_raw: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]



@lru_cache
def get_settings() -> Settings:
    s = get_settings_uncached()
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    return s


def get_settings_uncached() -> Settings:  # used by tests that patch env
    return Settings()
