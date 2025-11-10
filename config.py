# config.py
import os
from dotenv import load_dotenv
from typing import Optional, ClassVar, Self

load_dotenv()

class Settings:
    _instance: ClassVar[Optional["Settings"]] = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return

        self.database_url = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@localhost:5432/appdb")
        self.default_language = os.getenv("DEFAULT_LANGUAGE", "russian")
        self.whitelist = set(os.getenv("WHITELIST", "").split(','))
        self.bot_token = os.getenv("BOT_TOKEN")
        self.max_requests = int(os.getenv("MAX_REQUESTS", "10"))
        self.ban_threshold = int(os.getenv("BAN_THRESHOLD", "12"))
        self.ban_duration_seconds = int(os.getenv("BAN_DURATION_SECONDS", "600"))
        self.period = int(os.getenv("PERIOD", "5"))
        part_limit_mb = float(os.getenv("SUBMISSION_FILE_PART_LIMIT_MB", "48"))
        part_limit_mb = max(1.0, part_limit_mb)
        self.submission_file_part_max_bytes = int(part_limit_mb * 1024 * 1024)

        self._initialized = True
