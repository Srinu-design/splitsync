from functools import lru_cache
from typing import Annotated, NamedTuple

from pydantic import PositiveInt, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Limits(NamedTuple):
    members: PositiveInt
    expense_rows: PositiveInt
    settlements: PositiveInt


class Settings(BaseSettings):
    # DECISION: Load local .env values, requiring explicit database and Redis URLs.
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")

    DATABASE_URL: str
    REDIS_URL: str
    ALLOWED_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:8000"]
    ENABLE_NAIVE_MODE: bool = True
    NAIVE_RACE_DELAY_MS: int = 150
    DEMO_LIMITS: Annotated[Limits, NoDecode] = Limits(12, 200, 500)
    GROUP_LIMITS: Annotated[Limits, NoDecode] = Limits(50, 10000, 10000)
    DEMO_TTL_HOURS: int = 24
    GROUP_TTL_DAYS: int = 365
    MAX_ACTIVE_DEMO_ROOMS: int = 500
    MAX_ACTIVE_GROUPS: int = 1000
    MAX_DB_SIZE_MB: int = 4096
    EXACT_MAX_NONZERO: int = 15
    RATE_LIMIT_ENABLED: bool = True
    LOG_LEVEL: str = "INFO"
    EXPOSE_WORKER_PID: bool = False

    @field_validator("DEMO_LIMITS", "GROUP_LIMITS", mode="before")
    @classmethod
    def parse_limits(cls, value):
        # DECISION: Require exactly three positive caps; Pydantic validates the tuple fields.
        return tuple(part.strip() for part in value.split(",")) if isinstance(value, str) else value

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def parse_origins(cls, value):
        # DECISION: Trim comma-separated origins and discard empty entries.
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
