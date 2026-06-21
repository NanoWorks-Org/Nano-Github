from functools import cached_property
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    discord_token: str
    github_webhook_secret: str
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    database_url: str | None = None
    database_path: str = "data/nano_github.sqlite3"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @cached_property
    def sqlite_path(self) -> Path:
        if self.database_url:
            if not self.database_url.startswith("sqlite:///"):
                raise ValueError("Only sqlite:/// DATABASE_URL values are supported for now.")
            return Path(self.database_url.removeprefix("sqlite:///"))

        return Path(self.database_path)


settings = Settings()

