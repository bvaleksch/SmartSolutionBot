# bot/services/page.py
from __future__ import annotations

from uuid import UUID
from typing import ClassVar, Dict, List, Optional, Self, Tuple
from pathlib import Path

from smart_solution.db.database import DataBase
from smart_solution.db.schemas.page import (
    PageCreate,
    PageRead,
    PageUpdate,
)
from smart_solution.db.enums import PageType
from smart_solution.bot.services.audit_log import instrument_service_class


class PageService:
    """
    Singleton service for pages.
    This service does **not** use sessions or ORM models directly — only DataBase facade.
    Caches PageRead DTOs for quick repeated access.
    """
    _instance: ClassVar[Optional["PageService"]] = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return

        self._database: DataBase = DataBase()
        self._pages_by_id: Dict[UUID, PageRead] = {}
        # Keyed by (competition_id, slug, language_id)
        self._page_index: Dict[Tuple[UUID, str, Optional[UUID]], UUID] = {}
        self._pages_by_competition: Dict[UUID, List[UUID]] = {}
        self._pages_by_track: Dict[UUID, List[UUID]] = {}
        self._content_root = Path(__file__).resolve().parents[2] / "data" / "content"

        self._initialized = True

    # -----------------
    # Basic getters
    # -----------------
    async def get_page_by_id(self, page_id: UUID) -> Optional[PageRead]:
        page = self._pages_by_id.get(page_id)
        if page:
            return page
        page = await self._database.get_page_by_id(page_id)
        if page:
            self._cache_page(page)
        return page

    async def get_page(self, competition_id: UUID, slug: str, language_id: Optional[UUID] = None) -> Optional[PageRead]:
        key = (competition_id, slug, language_id)
        pid = self._page_index.get(key)
        if pid and pid in self._pages_by_id:
            return self._pages_by_id[pid]
        page = await self._database.get_page_by_slug(competition_id, slug, language_id)
        if page:
            self._cache_page(page)
        return page

    # -----------------
    # Listing
    # -----------------
    async def list_pages_by_competition(self, competition_id: UUID) -> List[PageRead]:
        ids = self._pages_by_competition.get(competition_id)
        if ids:
            out: List[PageRead] = []
            for pid in ids:
                p = self._pages_by_id.get(pid)
                if p is None:
                    out = []
                    break
                out.append(p)
            if out:
                return out
        pages = await self._database.list_pages_by_competition(competition_id)
        for p in pages:
            self._cache_page(p)
        return pages

    async def list_pages_by_track(self, track_id: UUID) -> List[PageRead]:
        ids = self._pages_by_track.get(track_id)
        if ids:
            out: List[PageRead] = []
            for pid in ids:
                p = self._pages_by_id.get(pid)
                if p is None:
                    out = []
                    break
                out.append(p)
            if out:
                return out
        pages = await self._database.list_pages_by_track(track_id)
        for p in pages:
            self._cache_page(p)
        return pages

    # -----------------
    # Mutations
    # -----------------
    async def create_page(self, payload: PageCreate) -> PageRead:
        page = await self._database.create_page(payload)
        self._cache_page(page)
        return page

    async def update_page(self, payload: PageUpdate) -> PageRead:
        page = await self._database.update_page(payload)
        self._cache_page(page)
        return page

    async def upsert_page(self, payload: PageCreate | PageUpdate) -> PageRead:
        if isinstance(payload, PageUpdate):
            return await self.update_page(payload)
        return await self.create_page(payload)

    # -----------------
    # Cache helpers
    # -----------------
    def _cache_page(self, page: PageRead) -> None:
        self._pages_by_id[page.id] = page
        if page.competition_id:
            key = (page.competition_id, page.slug, page.language_id)
            self._page_index[key] = page.id
        if page.competition_id:
            bucket = self._pages_by_competition.setdefault(page.competition_id, [])
            if page.id not in bucket:
                bucket.append(page.id)
        if page.track_id:
            bucket = self._pages_by_track.setdefault(page.track_id, [])
            if page.id not in bucket:
                bucket.append(page.id)

    def get_content_path(self, file_basename: str) -> Path:
        return self._content_root / file_basename


instrument_service_class(PageService, prefix="services.page")
