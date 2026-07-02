"""Application configuration loaded from environment variables."""
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The placeholder shipped in source. If production ever runs with this value,
# anyone who can read the repo can forge an Admin JWT — so we refuse to boot.
_DEFAULT_JWT_SECRET = "change-me-in-production-this-must-be-a-long-random-string"

# Passwords we know are weak/demo defaults; never allowed for prod seeding.
_WEAK_SEED_PASSWORDS = {
    "owner123", "admin123", "balaji123", "ranjith123",
    "password", "changeme", "secret", "admin", "owner",
}


class Settings(BaseSettings):
    """Strongly-typed settings; values come from `.env` or the OS environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # App
    app_name: str = Field(default="SK-POS Support")
    app_env: str = Field(default="development")

    # Database
    database_url: str = Field(default="sqlite:///./sk-pos-care.db")
    # Postgres schema all tables live in. Defaults to "public" (production).
    # Set DB_SCHEMA=test on a staging backend to run an isolated parallel copy
    # of every table inside the SAME database — production data is untouched.
    # Ignored on SQLite (no schema concept there).
    db_schema: str = Field(default="public")
    # Pool sizing — keep low so a single dev process can't exhaust Supabase's
    # session-mode pooler (which caps at 15 client connections). Production
    # behind the transaction-mode pooler (port 6543) can safely raise these.
    db_pool_size: int = Field(default=3)
    db_max_overflow: int = Field(default=2)
    db_pool_recycle_seconds: int = Field(default=300)

    # CORS
    cors_origins: str = Field(default="http://localhost:3000")

    # SMTP
    smtp_host: str = Field(default="smtp.gmail.com")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from_name: str = Field(default="SK-POS Support Support")
    smtp_from_email: str = Field(default="")
    support_inbox: str = Field(default="support@test.com")

    # Business rules
    duplicate_window_hours: int = Field(default=48)

    # Storage
    storage_backend: str = Field(default="local")  # "local" or "supabase"
    local_upload_dir: str = Field(default="./uploads")

    # Supabase Storage (only required when storage_backend="supabase")
    supabase_url: str = Field(default="")          # e.g. https://abcd1234.supabase.co
    supabase_service_key: str = Field(default="")  # the "service_role" secret from API settings
    supabase_bucket: str = Field(default="")       # e.g. sk-pos-care-uploads
    supabase_signed_url_ttl_seconds: int = Field(default=604800)  # 7 days

    # WhatsApp notifications via Twilio — optional staff alerts on new ticket.
    # Silent no-op when any of the first three are blank, so the app deploys
    # cleanly before Twilio has been configured.
    twilio_account_sid: str = Field(default="")
    twilio_auth_token: str = Field(default="")
    # Sender address, e.g. "whatsapp:+14155238886" for the Twilio Sandbox.
    twilio_whatsapp_from: str = Field(default="")
    # Optional approved content-template SID for the NEW-TICKET alert
    # (owners/managers). When blank, messages are sent as plain text (works with
    # Twilio Sandbox once recipients have opted in with "join {sandbox-keyword}").
    twilio_content_sid: str = Field(default="")
    # Optional approved content-template SID for the ENGINEER-ASSIGNMENT alert.
    # Separate template because the assignment message has its own layout/vars
    # ({{1}}..{{6}} = ref, location, category, severity, assigned-by, link).
    # When blank, the assignment alert falls back to plain text (Sandbox only).
    twilio_assignment_content_sid: str = Field(default="")
    # Base for the smart-redirect link embedded in the message body,
    # e.g. https://arcks-care-ticketting-system-fe.vercel.app
    whatsapp_link_base: str = Field(default="")

    # Auth
    jwt_secret: str = Field(default="change-me-in-production-this-must-be-a-long-random-string")
    jwt_algorithm: str = Field(default="HS256")
    jwt_expires_hours: int = Field(default=24)

    # Seed users (created on first boot if `users` table is empty)
    seed_owner_username: str = Field(default="owner")
    seed_owner_password: str = Field(default="owner123")
    seed_owner_name: str = Field(default="Admin")
    seed_manager_username: str = Field(default="admin")
    seed_manager_password: str = Field(default="admin123")
    seed_manager_name: str = Field(default="Manager")

    # Customer signing
    customer_sign_url_base: str = Field(default="http://localhost:3000")
    customer_sign_token_ttl_days: int = Field(default=30)

    @field_validator("cors_origins")
    @classmethod
    def _strip_origins(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        """True when APP_ENV marks this as a live deployment."""
        return self.app_env.strip().lower() in {"production", "prod", "live"}

    @model_validator(mode="after")
    def _enforce_production_security(self) -> "Settings":
        """Fail closed: a production deploy must not run on insecure defaults.

        JWT secret is required on every request, so it must always be strong in
        prod. Seed-password strength is enforced separately, at seed time, so a
        non-empty prod DB still boots regardless of the seed-password config.
        """
        if self.is_production:
            problems: List[str] = []
            if self.jwt_secret == _DEFAULT_JWT_SECRET:
                problems.append("JWT_SECRET is still the placeholder default")
            if len(self.jwt_secret) < 32:
                problems.append("JWT_SECRET must be at least 32 characters")
            if problems:
                raise ValueError(
                    "Refusing to start with APP_ENV=production:\n  - "
                    + "\n  - ".join(problems)
                    + "\nSet a strong JWT_SECRET (e.g. `openssl rand -hex 32`)."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor - safe to call from anywhere."""
    return Settings()
