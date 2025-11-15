# bot/services/submission.py
from uuid import UUID
from typing import Self, ClassVar, Optional, Dict, List, Tuple
from smart_solution.db.database import DataBase
from smart_solution.db.schemas.submission import SubmissionRead, SubmissionUpdate, SubmissionCreate
from smart_solution.db.schemas.user import UserRead
from smart_solution.db.schemas.team import TeamRead
from smart_solution.db.enums import SubmissionStatus
from smart_solution.bot.services.auto_judge import auto_judge
from smart_solution.bot.services.submission_notifications import submission_notifier
from smart_solution.bot.services.audit_log import instrument_service_class

class SubmissionService:
	_instance: ClassVar[Optional["SubmissionService"]] = None

	def __new__(cls, *args, **kwargs) -> Self:
		if cls._instance is None:
			cls._instance = super().__new__(cls)
		return cls._instance

	def __init__(self) -> None:
		if getattr(self, "_initialized", False):
			return

		self._initialized = True
		from smart_solution.bot.services.team import TeamService

		self._database = DataBase()
		self._submission: Dict[UUID, Optional[SubmissionRead]] = dict()
		self._team_submissions: Dict[UUID, List[UUID]] = dict()
		self._team_svc = TeamService()

	async def get_submission(self, sub_id: UUID) -> Optional[SubmissionRead]:
		if (sub_id in self._submission):
			return self._submission.get(sub_id)

		submission = await self._database.get_submission_by_id(sub_id)
		if submission is None:
			raise ValueError("Submission is not found")

		self._submission[sub_id] = submission

		team_user = await self._team_svc.get_team_user(submission.team_user_id)
		if team_user is None:
			raise RuntimeError("Team user is not found")
		if team_user.team_id not in self._team_submissions:
			self._team_submissions[team_user.team_id] = []
		if submission.id not in self._team_submissions[team_user.team_id]:
			self._team_submissions[team_user.team_id].append(submission.id)

		return submission

	async def get_user_by_submission(self, submission: SubmissionRead) -> UserRead:
		return await self._team_svc.get_user_by_team_user_id(submission.team_user_id)

	async def get_team_submissions(self, team: TeamRead) -> List[SubmissionRead]:
		if team.id not in self._team_submissions:
			submissions = await self._database.list_submissions_by_team(team.id)
			for s in submissions:
				self._submission[s.id] = s
			self._team_submissions[team.id] = [s.id for s in submissions]
		return self._team_submissions.get(team.id, [])

	@submission_notifier.notify_update()
	async def update_submission(self, submission: SubmissionUpdate) -> SubmissionRead:
		current_submission = await self.get_submission(submission.id)
		if current_submission is None:
			raise ValueError("Submission is not found")

		team_user = await self._team_svc.get_team_user(current_submission.team_user_id)
		if team_user is None:
			raise ValueError("Team user is not found")
		updated_submission = await self._database.upsert_submission(submission)
		self._submission[updated_submission.id] = updated_submission

		return updated_submission

	@auto_judge.auto_evaluate_create()
	async def create_submission(self, submission: SubmissionCreate) -> SubmissionRead:
		team_user = await self._team_svc.get_team_user(submission.team_user_id)
		if team_user is None:
			raise ValueError("Team user is not found")
		new_submission = await self._database.upsert_submission(submission)
		self._submission[new_submission.id] = new_submission

		if team_user.team_id not in self._team_submissions:
			self._team_submissions[team_user.team_id] = []
		if new_submission.id not in self._team_submissions[team_user.team_id]:
			self._team_submissions[team_user.team_id].append(new_submission.id)

		return new_submission

	async def upsert_submission(self, submission: SubmissionUpdate | SubmissionCreate) -> SubmissionRead:
		if type(submission) is SubmissionUpdate:
			return await self.update_submission(submission)
		return await self.create_submission(submission)

	async def count_submissions(self, team: TeamRead) -> int:
		return len(await self.get_team_submissions(team))

	async def list_submissions_page(
		self,
		page: int,
		page_size: int,
		status: SubmissionStatus | None = None,
	) -> Tuple[List[SubmissionRead], int]:
		page_index = max(0, int(page))
		limit = max(0, int(page_size))
		offset = page_index * limit if limit else 0
		items, total = await self._database.list_submissions_page(
			limit=limit,
			offset=offset,
			status=status,
		)
		for sub in items:
			self._submission[sub.id] = sub
		return items, total


instrument_service_class(SubmissionService, prefix="services.submission")
