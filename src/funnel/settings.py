"""Настройки процесса из переменных окружения и `.env`."""

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    # NoDecode: pydantic-settings не пытается разобрать строку как JSON, делим по запятой сами.
    admin_ids: Annotated[frozenset[int], NoDecode]
    webapp_url: str | None = None
    proxy_url: str | None = None
    http_port: int = Field(default=8080, ge=1, le=65535)
    db_path: Path = Path("data/funnel.db")
    funnel_config: Path = Path("config/funnel.yaml")
    log_level: str = "INFO"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts:
            raise ValueError("нужен хотя бы один Telegram ID")
        return frozenset(int(part) for part in parts)

    @field_validator("webapp_url", "proxy_url", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("webapp_url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("Mini App открывается только по HTTPS")
        return value

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()
