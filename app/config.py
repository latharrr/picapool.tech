from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Required — app refuses to start if missing
    dashboard_password: str
    secret_key: str

    # Database — Railway injects postgres:// or postgresql://, we normalise to asyncpg
    database_url: str = "postgresql+asyncpg://tracker:tracker@localhost/tracker"

    # Google Sheets — Railway can't mount files, so accept raw JSON string.
    # Set exactly one of these; GOOGLE_CREDENTIALS_JSON takes priority.
    google_credentials_json: str = ""             # raw JSON string (Railway)
    google_sheets_credentials_path: str = "service_account.json"  # file path (local)
    google_sheet_id: str = ""
    sheets_batch_interval: int = 10  # seconds between Sheets flushes

    ignored_threshold_hours: int = 48  # hours of silence before a link is "ignored"

    @field_validator("database_url", mode="before")
    @classmethod
    def fix_db_scheme(cls, v: str) -> str:
        """Normalise plain postgres(ql):// → postgresql+asyncpg:// for asyncpg."""
        if v.startswith("postgres://"):
            return "postgresql+asyncpg://" + v[len("postgres://"):]
        if v.startswith("postgresql://") and "+asyncpg" not in v:
            return "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v

    class Config:
        env_file = ".env"


settings = Settings()
