from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Required — app refuses to start without these
    dashboard_password: str
    secret_key: str
    google_sheet_id: str          # Sheets IS the database now

    # Google credentials — one of these must be set
    google_credentials_json: str = ""             # raw JSON string (Railway)
    google_sheets_credentials_path: str = "service_account.json"  # file (local)

    # Tuning
    sheets_batch_interval: int = 10   # seconds between event flush to Sheets
    cache_ttl_seconds: int = 30       # how often to refresh in-memory Sheets cache
    ignored_threshold_hours: int = 48

    # Optional AI
    groq_api_key: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
