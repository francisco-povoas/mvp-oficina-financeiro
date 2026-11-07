from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "mvp-oficina-financeiro"
    service_version: str = "0.1.0"
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    aws_region: str = "us-east-1"
    dynamodb_endpoint_url: str | None = None
    dynamodb_table: str = ""
    jwt_secret_key: str = "unsafe"
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_issuer: str = "mvp-oficina-ordens"
    jwt_user_audience: str = "mvp-oficina-users"

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    def validate_runtime(self) -> None:
        if not self.dynamodb_table:
            raise ValueError("DYNAMODB_TABLE must be provided at runtime")
        if self.environment == "prod" and self.dynamodb_endpoint_url is not None:
            raise ValueError("DYNAMODB_ENDPOINT_URL must be omitted in production")
        if self.environment == "prod" and self.jwt_secret_key == "unsafe":
            raise ValueError("JWT_SECRET_KEY must be provided in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
