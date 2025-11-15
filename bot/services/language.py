# bot/services/language.py
from uuid import UUID
from typing import Self, ClassVar, Dict, Optional, Union, Iterable

from smart_solution.db.database import DataBase
from smart_solution.config import Settings
from smart_solution.db.schemas.language import LanguageRead
from smart_solution.bot.services.audit_log import instrument_service_class


class LanguageService:
    """
    Singleton service that resolves languages by UUID or by name and returns LanguageRead DTOs.
    Adds bulk listing and cache warm-up methods.
    """

    _instance: ClassVar[Optional["LanguageService"]] = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._by_id: Dict[UUID, LanguageRead] = {}
        self._by_name: Dict[str, LanguageRead] = {}
        self._database: DataBase = DataBase()
        self._initialized = True

    # -----------------------
    # Internal helpers
    # -----------------------
    @staticmethod
    def _normalize_name(name: str) -> str:
        return name.strip().lower()

    def _put_cache(self, lang: LanguageRead) -> None:
        if getattr(lang, "id", None):
            self._by_id[lang.id] = lang
        if getattr(lang, "name", None):
            self._by_name[self._normalize_name(lang.name)] = lang

    def _put_many(self, langs: Iterable[LanguageRead]) -> None:
        for lang in langs:
            self._put_cache(lang)

    # -----------------------
    # Public lookups
    # -----------------------
    async def get_by_id(self, lang_id: Optional[UUID]) -> Optional[LanguageRead]:
        if not lang_id:
            return None
        cached = self._by_id.get(lang_id)
        if cached:
            return cached
        lang = await self._database.get_language_by_id(lang_id)
        if lang:
            self._put_cache(lang)
        return lang

    async def get_by_name(self, language_name: Optional[str]) -> Optional[LanguageRead]:
        if not language_name:
            return None
        key = self._normalize_name(language_name)
        cached = self._by_name.get(key)
        if cached:
            return cached
        lang = await self._database.get_language_by_name(language_name)
        if lang:
            self._put_cache(lang)
        return lang

    async def autoget(self, key: Union[str, UUID]) -> Optional[LanguageRead]:
        if isinstance(key, UUID):
            return await self.get_by_id(key)
        elif isinstance(key, str):
            return await self.get_by_name(key)
        else:
            return None

    async def safe_autoget(self, key: Union[str, UUID]) -> LanguageRead:
        lang = await self.autoget(key)
        if lang:
            return lang

        default_val = Settings().default_language
        if not default_val:
            raise LookupError("Default language is not configured (Settings().default_language)")

        # Try UUID first, then name
        fallback: Optional[LanguageRead] = None
        if isinstance(default_val, UUID):
            fallback = await self.get_by_id(default_val)
        else:
            try:
                fallback = await self.get_by_id(UUID(str(default_val)))
            except Exception:
                fallback = await self.get_by_name(str(default_val))

        if fallback:
            return fallback

        raise LookupError(
            f"Cannot resolve language for key={key!r} and default={default_val!r}. "
            f"Ensure language records exist."
        )

    # -----------------------
    # Bulk / cache utilities
    # -----------------------
    async def all_languages(self) -> list[LanguageRead]:
        """
        Return all languages ordered by name (case-insensitive).
        Also warms the cache for subsequent lookups.
        """
        langs = await self._database.list_languages()  # <- новый метод в DataBase
        self._put_many(langs)
        return langs

    async def refresh_cache(self) -> None:
        """
        Hard-refresh the cache from DB.
        """
        self._by_id.clear()
        self._by_name.clear()
        await self.list_all()

    async def ensure_cache(self) -> None:
        """
        Soft-warm the cache: if empty, populate it from DB.
        """
        if not self._by_id and not self._by_name:
            await self.list_all()

    async def get_map_by_name(self) -> Dict[str, LanguageRead]:
        """
        Ensure cache and return a name->LanguageRead map (keys are normalized lowercase).
        """
        await self.ensure_cache()
        return dict(self._by_name)

    async def get_map_by_id(self) -> Dict[UUID, LanguageRead]:
        """
        Ensure cache and return an id->LanguageRead map.
        """
        await self.ensure_cache()
        return dict(self._by_id)

instrument_service_class(LanguageService, prefix="services.language")

