from functools import lru_cache
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    public_base_url: str = "http://localhost:8010"
    database_path: str = "/data/db/people.sqlite"
    # The one password that opens the photographer's workspace.
    photographer_password: str = "people-local"
    secret_key: str = "local-only-change-this-before-any-public-deployment-7dfc23"
    storage_backend: str = "local"
    storage_root: str = "/data/storage"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    default_locale: str = "en"
    # Shown in the footers and the FAQ when set.
    contact_email: str = ""

    @property
    def is_local_url(self):
        host = urlparse(self.public_base_url).hostname or ""
        return host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".localhost", ".test", ".invalid"))

    def validate_runtime(self):
        if self.storage_backend not in ("local", "r2"):
            raise ValueError("STORAGE_BACKEND must be local or r2")
        if self.storage_backend == "r2" and not all(
            (self.r2_account_id, self.r2_access_key_id, self.r2_secret_access_key, self.r2_bucket)
        ):
            raise ValueError("R2 storage needs R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET")
        if self.app_env != "development":
            if "local-only" in self.secret_key or len(self.secret_key) < 32:
                raise ValueError("Set a strong SECRET_KEY")
            if self.photographer_password == "people-local" or len(self.photographer_password) < 12:
                raise ValueError("Set PHOTOGRAPHER_PASSWORD to a password of at least 12 characters")
            if not self.public_base_url.startswith("https://"):
                raise ValueError("Public deployment requires HTTPS")


@lru_cache
def settings():
    value = Settings()
    value.validate_runtime()
    return value
