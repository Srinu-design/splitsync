import pytest
from pydantic import ValidationError

from app.config import Limits, Settings, get_settings


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:dev@localhost:5433/splitsync")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6380/0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_every_spec_default():
    assert Settings(_env_file=None).model_dump() == {
        "DATABASE_URL": "postgresql+asyncpg://postgres:dev@localhost:5433/splitsync",
        "REDIS_URL": "redis://localhost:6380/0",
        "ALLOWED_ORIGINS": ["http://localhost:8000"],
        "ENABLE_NAIVE_MODE": True,
        "NAIVE_RACE_DELAY_MS": 150,
        "DEMO_LIMITS": (12, 200, 500),
        "GROUP_LIMITS": (50, 10000, 10000),
        "DEMO_TTL_HOURS": 24,
        "GROUP_TTL_DAYS": 365,
        "MAX_ACTIVE_DEMO_ROOMS": 500,
        "MAX_ACTIVE_GROUPS": 1000,
        "MAX_DB_SIZE_MB": 4096,
        "EXACT_MAX_NONZERO": 15,
        "RATE_LIMIT_ENABLED": True,
        "LOG_LEVEL": "INFO",
        "EXPOSE_WORKER_PID": False,
    }


def test_environment_parses_csv_and_scalars(monkeypatch):
    monkeypatch.setenv("DEMO_LIMITS", " 3, 20, 50 ")
    monkeypatch.setenv("GROUP_LIMITS", "40,9000,8000")
    monkeypatch.setenv("ALLOWED_ORIGINS", " http://localhost:8300, , https://example.test ")
    monkeypatch.setenv("ENABLE_NAIVE_MODE", "false")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("EXPOSE_WORKER_PID", "true")
    monkeypatch.setenv("NAIVE_RACE_DELAY_MS", "200")
    settings = Settings(_env_file=None)
    assert settings.DEMO_LIMITS == Limits(3, 20, 50)
    assert settings.GROUP_LIMITS == Limits(40, 9000, 8000)
    assert settings.DEMO_LIMITS.members == 3
    assert settings.DEMO_LIMITS.expense_rows == 20
    assert settings.DEMO_LIMITS.settlements == 50
    assert settings.ALLOWED_ORIGINS == ["http://localhost:8300", "https://example.test"]
    assert settings.ENABLE_NAIVE_MODE is False
    assert settings.RATE_LIMIT_ENABLED is False
    assert settings.EXPOSE_WORKER_PID is True
    assert settings.NAIVE_RACE_DELAY_MS == 200


@pytest.mark.parametrize("field", ["DEMO_LIMITS", "GROUP_LIMITS"])
@pytest.mark.parametrize("value", ["1,2", "1,2,3,4", "a,2,3", "0,2,3", "1,-2,3", ""])
def test_invalid_limits_fail_validation(monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("field", ["DATABASE_URL", "REDIS_URL"])
def test_connection_urls_are_required(monkeypatch, field):
    monkeypatch.delenv(field)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_dotenv_with_local_values_and_environment_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ALLOWED_ORIGINS=http://localhost:8300\nDEMO_LIMITS=4,30,60\nLOG_LEVEL=DEBUG\n"
    )
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    settings = Settings(_env_file=env_file)
    assert settings.ALLOWED_ORIGINS == ["http://localhost:8300"]
    assert settings.DEMO_LIMITS == Limits(4, 30, 60)
    assert settings.LOG_LEVEL == "WARNING"


def test_settings_are_cached(monkeypatch):
    first = get_settings()
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().LOG_LEVEL == "DEBUG"
