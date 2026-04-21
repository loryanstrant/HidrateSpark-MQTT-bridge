"""Configuration model with YAML round-trip persistence."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class MqttConfig(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    base_topic: str = "hidrate"
    client_id: str = "hidrate-mqtt-bridge"
    ha_discovery: bool = True
    ha_discovery_prefix: str = "homeassistant"


class BottleConfig(BaseModel):
    mac: str = ""
    name_prefix: str = "h2o"
    size_ml: int = 591
    poll_interval_s: int = 30


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class RuntimeState(BaseModel):
    """Persisted runtime state (sip totals, fill estimate)."""
    current_fill_ml: Optional[int] = None
    lifetime_total_ml: int = 0
    last_refill_ts: Optional[float] = None
    today_date: str = ""  # YYYY-MM-DD; resets total_today across days
    total_today_ml: int = 0
    # Weight calibration: raw low-byte reading observed when bottle is full.
    # Captured automatically after each detected refill. 1 unit ~ 1 mL.
    weight_full_low: Optional[int] = None


class Settings(BaseModel):
    mqtt: MqttConfig = Field(default_factory=MqttConfig)
    bottle: BottleConfig = Field(default_factory=BottleConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    runtime: RuntimeState = Field(default_factory=RuntimeState)


def config_path() -> Path:
    return Path(os.environ.get("CONFIG_PATH", "./config/config.yaml"))


def load_settings() -> Settings:
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        s = Settings()
        save_settings(s)
        return s
    with path.open("r") as f:
        data = yaml.safe_load(f) or {}
    return Settings.model_validate(data)


def save_settings(settings: Settings) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(settings.model_dump(), f, sort_keys=False)
