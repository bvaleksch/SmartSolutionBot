"""Infrastructure for registering and executing automatic submission scorers.

The :class:`AutoJudgeService` acts as a singleton registry keyed by track slug.
Scorers can be registered via the :meth:`AutoJudgeService.register` decorator and
are automatically invoked when a submission is created. Results propagate back to
the database and are stored temporarily for user-facing notifications.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Awaitable, Callable, ClassVar, Dict, Optional, Protocol
logger = logging.getLogger(__name__)
from smart_solution.db.enums import SubmissionStatus, SortDirection
from smart_solution.db.schemas.submission import SubmissionCreate, SubmissionRead, SubmissionUpdate
from smart_solution.db.schemas.team import TeamRead
from smart_solution.db.schemas.track import TrackRead


@dataclass(slots=True)
class AutoJudgeResult:
	"""Container describing the outcome of an automated evaluation."""
	status: SubmissionStatus | None = None
	value: float | None = None
	message: str | None = None
	success: bool = True


class _Scorer(Protocol):
	async def __call__(
		self,
		file_path: Path,
		submission: SubmissionRead,
		team: TeamRead,
		track: TrackRead,
	) -> Optional[AutoJudgeResult]: ...


class AutoJudgeService:
	"""Singleton registry that manages automatic submission scorers."""

	_instance: ClassVar[Optional["AutoJudgeService"]] = None

	def __new__(cls) -> "AutoJudgeService":
		if cls._instance is None:
			cls._instance = super().__new__(cls)
		return cls._instance

	def __init__(self) -> None:
		if getattr(self, "_initialized", False):
			return

		self._scorers: Dict[str, _Scorer] = {}
		self._submission_service = None
		self._results: Dict[uuid.UUID, AutoJudgeResult] = {}
		self._judge_lock: asyncio.Lock | None = None
		self._initialized = True

	def register(self, track_slug: str) -> Callable[[_Scorer], _Scorer]:
		"""Return a decorator that registers a scorer for the given track slug."""
		slug = track_slug.strip().lower()

		def _decorator(func: _Scorer) -> _Scorer:
			self._scorers[slug] = func
			return func

		return _decorator

	def auto_evaluate_create(self) -> Callable[[Callable[..., Awaitable[SubmissionRead]]], Callable[..., Awaitable[SubmissionRead]]]:
		"""Decorator that wraps ``SubmissionService.create_submission`` for auto-scoring."""
		def _decorator(func: Callable[..., Awaitable[SubmissionRead]]) -> Callable[..., Awaitable[SubmissionRead]]:
			async def _wrapper(service_self, *args, **kwargs):
				submission_payload: Optional[SubmissionCreate] = None
				if args:
					submission_payload = next((arg for arg in args if isinstance(arg, SubmissionCreate)), None)
				if submission_payload is None:
					for value in kwargs.values():
						if isinstance(value, SubmissionCreate):
							submission_payload = value
							break

				created = await func(service_self, *args, **kwargs)

				if submission_payload is not None:
					try:
						await self._auto_from_submission(created, submission_payload)
					except Exception:
						pass

				return created

			return _wrapper

		return _decorator

	async def evaluate_submission(
		self,
		*,
		submission: SubmissionRead,
		team: TeamRead,
		track: TrackRead,
		file_path: Path,
	) -> Optional[AutoJudgeResult]:
		"""Run the registered scorer for a submission and persist the result."""
		slug = (track.slug or "").strip().lower()
		scorer = self._scorers.get(slug)
		if scorer is None:
			logger.debug("No auto-judge registered for track slug '%s'", slug)
			return None

		try:
			result = scorer(file_path, submission, team, track)
			if asyncio.iscoroutine(result):
				result = await result
		except Exception:
			logger.exception("Auto-judge scorer failed for submission %s", submission.id)
			return AutoJudgeResult(success=False)

		if result is None:
			return None

		update: Dict[str, object] = {}
		if result.value is not None:
			update["value"] = result.value
		if result.status is not None:
			update["status"] = result.status
		if update:
			try:
				await self._get_submission_service().update_submission(
					SubmissionUpdate(id=submission.id, **update)  # type: ignore[arg-type]
				)
			except Exception:
				logger.exception("Failed to persist auto-judge result for submission %s", submission.id)
				return AutoJudgeResult(success=False)

		return result

	def pop_result(self, submission_id: uuid.UUID) -> Optional[AutoJudgeResult]:
		"""Return and remove a cached :class:`AutoJudgeResult` for user notification."""
		return self._results.pop(submission_id, None)

	def _get_submission_service(self):
		"""Lazy accessor ensuring ``SubmissionService`` is only instantiated when needed."""
		if self._submission_service is None:
			from smart_solution.bot.services.submission import SubmissionService

			self._submission_service = SubmissionService()
		return self._submission_service

	def _get_judge_lock(self) -> asyncio.Lock:
		lock = self._judge_lock
		if lock is None:
			lock = asyncio.Lock()
			self._judge_lock = lock
		return lock

	async def _auto_from_submission(self, created: SubmissionRead, payload: SubmissionCreate) -> None:
		"""Resolve the submission context and trigger a scorer if one is registered."""
		from smart_solution.bot.services.team import TeamService
		from smart_solution.bot.services.competition import CompetitionService

		team_svc = TeamService()
		membership = await team_svc.get_team_user(created.team_user_id)
		if membership is None:
			return
		team = await team_svc.get_team(membership.team_id)
		if team is None or team.track_id is None:
			return

		comp_svc = CompetitionService()
		track = await comp_svc.get_track_by_id(team.track_id)
		if track is None:
			return

		file_path = Path(payload.file_path)
		if not file_path.is_absolute():
			base = Path(__file__).resolve().parents[2]
			candidates = [base / file_path]
			data_dir = base / "data"
			if not file_path.parts or file_path.parts[0] != "data":
				candidates.insert(0, data_dir / file_path)
			for candidate in candidates:
				if candidate.exists():
					file_path = candidate
					break
			else:
				logger.warning(
					"Submission file %s not found for auto-judge; checked %s",
					payload.file_path,
					", ".join(str(c) for c in candidates),
				)
				return
		elif not file_path.exists():
			logger.warning("Submission file %s not found (absolute path)", file_path)
			return

		if track.sort_by:
			try:
				SortDirection(track.sort_by)
			except ValueError:
				pass

		lock = self._get_judge_lock()
		async with lock:
			result = await self.evaluate_submission(
				submission=created,
				team=team,
				track=track,
				file_path=file_path,
			)
		if result is not None:
			self._results[created.id] = result
			logger.info(
				"Auto-judge stored result for submission %s: success=%s status=%s value=%s",
				created.id,
				result.success,
				getattr(result.status, "value", result.status),
				result.value,
			)


auto_judge = AutoJudgeService()

__all__ = ["AutoJudgeResult", "AutoJudgeService", "auto_judge"]
