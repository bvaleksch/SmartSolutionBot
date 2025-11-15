# bot/services/competition.py
from uuid import UUID
from typing import ClassVar, Dict, List, Optional, Self

from smart_solution.db.database import DataBase
from smart_solution.db.schemas.competition import (
    CompetitionCreate,
    CompetitionRead,
    CompetitionUpdate,
)
from smart_solution.db.schemas.track import (
    ShortTrackInfo,
    TrackCreate,
    TrackRead,
    TrackUpdate,
    TrackLeaderboardRow,
)
from smart_solution.bot.services.audit_log import instrument_service_class


class CompetitionService:
	"""
	Singleton service layer for competitions and tracks.

	Strict rule: this service does **not** touch SQLAlchemy sessions or models.
	It only calls the DataBase facade and returns DTOs.
	"""

	_instance: ClassVar[Optional["CompetitionService"]] = None

	def __new__(cls) -> Self:
		if cls._instance is None:
			cls._instance = super().__new__(cls)
		return cls._instance

	def __init__(self) -> None:
		if getattr(self, "_initialized", False):
			return

		self._database: DataBase = DataBase()

		# Caches (DTO-based)
		self._competitions: Dict[UUID, CompetitionRead] = {}
		self._competition_by_slug: Dict[str, UUID] = {}
		self._tracks: Dict[UUID, TrackRead] = {}
		self._tracks_by_competition: Dict[UUID, List[UUID]] = {}

		self._initialized = True

	# --------------------
	# Competition methods
	# --------------------
	async def get_competition_by_id(self, comp_id: UUID) -> Optional[CompetitionRead]:
		if comp_id in self._competitions:
			return self._competitions[comp_id]
		comp = await self._database.get_competition_by_id(comp_id)
		if comp:
			self._cache_competition(comp)
		return comp

	async def get_competition(self, slug: str) -> Optional[CompetitionRead]:
		if slug in self._competition_by_slug:
			cid = self._competition_by_slug[slug]
			if cid in self._competitions:
				return self._competitions[cid]
		comp = await self._database.get_competition_by_slug(slug)
		if comp:
			self._cache_competition(comp)
		return comp

	async def create_competition(self, payload: CompetitionCreate) -> CompetitionRead:
		comp = await self._database.create_competition(payload)
		self._cache_competition(comp)
		return comp

	async def list_competitions_page(self, page: int, page_size: int) -> tuple[list[CompetitionRead], int]:
		limit = page_size
		offset = max(page, 0) * page_size
		items, total = await self._database.list_competitions(limit=limit, offset=offset)
		for comp in items:
			self._cache_competition(comp)
		return items, total

	async def update_competition(self, payload: CompetitionUpdate) -> CompetitionRead:
		comp = await self._database.update_competition(payload)
		self._cache_competition(comp)
		return comp

	async def upsert_competition(self, payload: CompetitionCreate | CompetitionUpdate) -> CompetitionRead:
		if isinstance(payload, CompetitionUpdate):
			return await self.update_competition(payload)
		return await self.create_competition(payload)

	# -------------
	# Track methods
	# -------------
	async def get_track_by_id(self, track_id: UUID) -> Optional[TrackRead]:
		if track_id in self._tracks:
			return self._tracks[track_id]
		tr = await self._database.get_track_by_id(track_id)
		if tr:
			self._cache_track(tr)
		return tr

	async def list_tracks(self, competition_id: UUID) -> List[TrackRead]:
		ids = self._tracks_by_competition.get(competition_id)
		if ids:
			tracks: List[TrackRead] = []
			for tid in ids:
				t = self._tracks.get(tid)
				if t is None:
					tracks = []
					break
				tracks.append(t)
			if tracks:
				return tracks

		tracks = await self._database.list_tracks_by_competition(competition_id)
		for t in tracks:
			self._cache_track(t)
		return tracks

	async def create_track(self, payload: TrackCreate) -> TrackRead:
		tr = await self._database.create_track(payload)
		self._cache_track(tr)
		return tr

	async def update_track(self, payload: TrackUpdate) -> TrackRead:
		tr = await self._database.update_track(payload)
		self._cache_track(tr)
		return tr

	async def upsert_track(self, payload: TrackCreate | TrackUpdate) -> TrackRead:
		if isinstance(payload, TrackUpdate):
			return await self.update_track(payload)
		return await self.create_track(payload)

	# -----------------
	# Helper utilities
	# -----------------
	async def get_short_track_info(self, track: TrackRead | UUID) -> ShortTrackInfo:
		if isinstance(track, UUID):
			track = await self.get_track_by_id(track)

		comp = await self.get_competition_by_id(track.competition_id)
		if comp is None:
			raise LookupError(f"Competition {track.competition_id} not found for track {track.id}")
		return ShortTrackInfo(
			slug=track.slug,
			competition_title=comp.title,
			track_title=track.title,
			start_at=comp.start_at,
			end_at=comp.end_at,
		)

	async def max_count_submission(self, track: TrackRead) -> int:
		return int(track.max_submissions_total)


	async def get_track_leaderboard(self, track_id: UUID) -> tuple[Optional[TrackRead], Optional[CompetitionRead], list[TrackLeaderboardRow]]:
		track = await self.get_track_by_id(track_id)
		if track is None:
			return None, None, []
		comp = await self.get_competition_by_id(track.competition_id)
		rows = await self._database.leaderboard_for_track(track.id, track.sort_by)
		return track, comp, rows

	async def max_count_submission_by_track_id(self, track_id: UUID) -> int:
		return await self.max_count_submission(await self.get_track_by_id(track_id))

	# ---------------
	# Cache helpers
	# ---------------
	def _cache_competition(self, comp: CompetitionRead) -> None:
		self._competitions[comp.id] = comp
		self._competition_by_slug[comp.slug] = comp.id

	def _cache_track(self, tr: TrackRead) -> None:
		self._tracks[tr.id] = tr
		bucket = self._tracks_by_competition.setdefault(tr.competition_id, [])
		if tr.id not in bucket:
			bucket.append(tr.id)


instrument_service_class(CompetitionService, prefix="services.competition")
