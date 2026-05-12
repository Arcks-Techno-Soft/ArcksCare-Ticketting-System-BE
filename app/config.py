"""Application configuration loaded from environment variables."""
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed settings; values come from `.env` or the OS environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # App
    app_name: str = Field(default="ArcksCare")
    app_env: str = Field(default="development")

    # Database
    database_url: str = Field(default="sqlite:///./arckscare.db")

    # CORS
    cors_origins: str = Field(default="http://localhost:3000")

    # SMTP
    smtp_host: str = Field(default="smtp.gmail.com")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from_name: str = Field(default="ArcksCare Support")
    smtp_from_email: str = Field(default="")
    support_inbox: str = Field(default="support@test.com")

    # Business rules
    duplicate_window_hours: int = Field(default=48)

    # Storage
    storage_backend: str = Field(default="local")
    local_upload_dir: str = Field(default="./uploads")

    @field_validator("cors_origins")
    @classmethod
    def _strip_origins(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor - safe to call from anywhere."""
    return Settings()
