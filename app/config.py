from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_ENV: str = "dev"
    APP_PORT: int = 8000

    BITRIX_WEBHOOK_URL: AnyHttpUrl
    BITRIX_PORTAL_URL: AnyHttpUrl

    SIPUNI_USER: str
    SIPUNI_SECRET: str
    SIPUNI_API_BASE: AnyHttpUrl = "https://sipuni.com/api"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
