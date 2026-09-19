from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MATHLAB_", env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{ROOT / 'backend' / 'mathlab.db'}"
    artifact_dir: Path = ROOT / "backend" / "artifacts"
    # Bootstrap owner key. Collaborators are managed through the private API.
    owner_api_key: str = "change-me-owner-key"
    public_base_url: str = "http://localhost:8000"

    # Devin provider: "mock" (deterministic local stand-in) or "api" (cloud v3 API).
    devin_provider: str = "mock"
    devin_api_base: str = "https://api.devin.ai"
    devin_api_key: str = ""
    devin_org_id: str = ""
    # Attribute API-created sessions to Sharon when authenticating as a service user.
    devin_create_as_user_id: str = ""
    devin_max_acu_limit: int | None = None

    # Lean project used for independent checking. Empty string disables Lean (checker reports
    # "checker_unavailable" rather than pretending to verify).
    lean_project_dir: Path = ROOT / "lean"
    lean_checker_url: str = ""
    lean_checker_token: str = ""
    lean_timeout_seconds: int = 300
    allowed_axioms: tuple[str, ...] = ("propext", "Classical.choice", "Quot.sound")

    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 30
    default_max_concurrent_sessions: int = 2

    cors_origins: tuple[str, ...] = ("http://localhost:5173",)

    @field_validator("devin_max_acu_limit", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
