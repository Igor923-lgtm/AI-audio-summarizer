from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr


class Settings(BaseSettings):
    telegram_bot_token: SecretStr
    groq_api_key: SecretStr
    # Telegram ID владельцев через запятую; пустая строка — whitelist отключён
    allowed_user_ids: str = ""
    notes_dir: str = "notes"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_ids(self) -> set[int]:
        if not self.allowed_user_ids.strip():
            return set()
        result = set()
        for part in self.allowed_user_ids.split(","):
            part = part.strip()
            if part.isdigit():
                result.add(int(part))
        return result


settings = Settings()
