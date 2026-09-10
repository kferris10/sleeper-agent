"""Load config.toml into typed settings."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class LeagueConfig(BaseModel):
    league_id: str = ""
    user_id: str = ""  # Sleeper user_id, not username (usernames change)
    timezone: str = "America/New_York"


class PhilosophyConfig(BaseModel):
    risk_tolerance: str = "medium"  # low | medium | high
    faab_aggressiveness: str = "medium"
    trade_appetite: str = "opportunistic"  # none | opportunistic | active
    priorities: list[str] = Field(default_factory=list)
    notes: str = ""


class ClaudeConfig(BaseModel):
    model: str = "claude-opus-5"
    max_tokens: int = 16000
    web_search: bool = True
    web_search_max_uses: int = 8


class DeliveryConfig(BaseModel):
    channel: str = "email"  # email | slack | discord | none
    email_to: str = ""
    email_from: str = ""  # default: smtp_user
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587  # STARTTLS
    smtp_user: str = ""  # default: email_to
    # Secrets live in the environment, not here:
    #   SMTP_PASSWORD (Gmail app password), WEBHOOK_URL (Slack/Discord)


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    league: LeagueConfig = Field(default_factory=LeagueConfig)
    philosophy: PhilosophyConfig = Field(default_factory=PhilosophyConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    byes: dict[str, int] = Field(default_factory=dict)  # NFL team code -> bye week
    data_dir: Path = Path("data")


def load_settings(path: Path | None = None) -> Settings:
    config_path = path or Path("config.toml")
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} not found. Copy config.example.toml to config.toml and fill in "
            "[league] (find your ids with: sleeper-analyst setup --username YOUR_NAME)"
        )
    # utf-8-sig tolerates the BOM that Windows editors/PowerShell often prepend
    raw = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    return Settings.model_validate(raw)
