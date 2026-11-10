"""Модель YAML-конфига воронки и его загрузка."""

from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
StepKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$")]

# Эти ключи заняты базовыми шагами воронки на дашборде.
RESERVED_KEYS = frozenset({"start", "phone"})


class ConfigError(Exception):
    """Конфиг не прочитался или не прошёл проверку."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Messages(_Strict):
    welcome: Text
    ask_phone: Text
    phone_button: Text
    registered: Text
    already_registered: Text
    not_own_contact: Text
    use_button: Text


class Followup(_Strict):
    key: StepKey
    delay_seconds: int = Field(gt=0)
    text: Text


class FunnelConfig(_Strict):
    parse_mode: Literal["HTML", "MarkdownV2"] | None = "HTML"
    messages: Messages
    followups: list[Followup] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_keys(self) -> Self:
        keys = [followup.key for followup in self.followups]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"ключи followups повторяются: {', '.join(duplicates)}")
        reserved = sorted(RESERVED_KEYS.intersection(keys))
        if reserved:
            raise ValueError(f"ключи followups заняты шагами воронки: {', '.join(reserved)}")
        return self

    def followup_text(self, key: str) -> str | None:
        for followup in self.followups:
            if followup.key == key:
                return followup.text
        return None


def _format_errors(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        path = ".".join(str(part) for part in item["loc"]) or "<корень>"
        lines.append(f"  {path}: {item['msg']}")
    return "\n".join(lines)


def load_config(path: Path) -> FunnelConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"не удалось прочитать {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: невалидный YAML: {exc}") from exc
    try:
        return FunnelConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path}: конфиг не прошёл проверку:\n{_format_errors(exc)}") from exc
