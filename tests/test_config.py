from pathlib import Path

import pytest
import yaml

from funnel.config import ConfigError, FunnelConfig, load_config


def write(*, tmp_path: Path, data: object) -> Path:
    path = tmp_path / "funnel.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def base(config: FunnelConfig) -> dict[str, object]:
    return config.model_dump()


def test_repo_config_loads(config: FunnelConfig) -> None:
    assert [f.key for f in config.followups] == ["gift", "warmup"]
    assert [f.delay_seconds for f in config.followups] == [10, 60]
    assert config.messages.phone_button == "📱 Отправить номер"


def test_third_followup_needs_no_code(tmp_path: Path, config: FunnelConfig) -> None:
    data = base(config)
    data["followups"] = [
        *config.model_dump()["followups"],
        {"key": "reminder", "delay_seconds": 3600, "text": "Скоро эфир"},
    ]
    loaded = load_config(write(tmp_path=tmp_path, data=data))
    assert [f.key for f in loaded.followups] == ["gift", "warmup", "reminder"]


def test_duplicate_keys_rejected(tmp_path: Path, config: FunnelConfig) -> None:
    data = base(config)
    data["followups"] = [{"key": "gift", "delay_seconds": 10, "text": "a"}] * 2
    with pytest.raises(ConfigError, match="повторяются: gift"):
        load_config(write(tmp_path=tmp_path, data=data))


@pytest.mark.parametrize("delay", [0, -5])
def test_non_positive_delay_rejected(tmp_path: Path, config: FunnelConfig, delay: int) -> None:
    data = base(config)
    data["followups"] = [{"key": "gift", "delay_seconds": delay, "text": "a"}]
    with pytest.raises(ConfigError, match=r"followups\.0\.delay_seconds"):
        load_config(write(tmp_path=tmp_path, data=data))


def test_empty_text_rejected(tmp_path: Path, config: FunnelConfig) -> None:
    data = base(config)
    data["messages"]["welcome"] = "   "  # type: ignore[index]
    with pytest.raises(ConfigError, match=r"messages\.welcome"):
        load_config(write(tmp_path=tmp_path, data=data))


def test_empty_followups_rejected(tmp_path: Path, config: FunnelConfig) -> None:
    data = base(config)
    data["followups"] = []
    with pytest.raises(ConfigError, match="followups"):
        load_config(write(tmp_path=tmp_path, data=data))


@pytest.mark.parametrize("key", ["Gift", "gift-1", "", "phone"])
def test_bad_key_rejected(tmp_path: Path, config: FunnelConfig, key: str) -> None:
    data = base(config)
    data["followups"] = [{"key": key, "delay_seconds": 10, "text": "a"}]
    with pytest.raises(ConfigError):
        load_config(write(tmp_path=tmp_path, data=data))


def test_unknown_field_rejected(tmp_path: Path, config: FunnelConfig) -> None:
    data = base(config)
    data["messages"]["welcom"] = "опечатка"  # type: ignore[index]
    with pytest.raises(ConfigError, match=r"messages\.welcom"):
        load_config(write(tmp_path=tmp_path, data=data))
