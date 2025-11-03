# bot/services/team.py
from uuid import UUID
from typing import Optional, ClassVar, Self, Any, Dict, Tuple, overload, List
from datetime import datetime
from smart_solution.db.schemas.user import UserRead
from smart_solution.db.schemas.team_user import TeamUserRead, TeamUserCreate, TeamUserUpdate
from smart_solution.db.schemas.team import TeamRead, TeamCreate, TeamUpdate
from smart_solution.db.database import DataBase
from smart_solution.bot.services.user import UserService
from smart_solution.bot.services.submission import SubmissionService
from smart_solution.bot.services.competition import CompetitionService


class MembershipDict:
	def __init__(self) -> None:
		self._memberships: Dict[Tuple[UUID, UUID], UUID] = dict() # Dict[(user_id, team_id), team_user_id]
		self._data: Dict[UUID, TeamUserRead] = dict()

	def upsert(self, membership: TeamUserRead | None) -> None:
		if membership is None:
			return 

		user_id = membership.user_id
		team_id = membership.team_id

		self._memberships[(user_id, team_id)] = membership.id
		self._data[membership.id] = membership

	def __contains__(self, key: UUID | Tuple[UUID, UUID]) -> bool:
		if type(key) is tuple:
			if key in self._memberships:
				uid = self._memberships.get(key)
			else:
				return False
		else:
			uid = key

		return uid in self._data

	def __getitem__(self, key: UUID | Tuple[UUID, UUID]) -> TeamUserRead:
		if key not in self:
			raise KeyError(key)

		if type(key) is tuple:
			uid = self._memberships.get(key)
		else:
			uid = key

		return self._data.get(uid)

	def get(self, key: UUID | Tuple[UUID, UUID], default: Any = None) -> TeamUserRead | Any:
		if key not in self:
			return default

		return self.__getitem__(key)


class TeamService:
	_instance: ClassVar[Optional["TeamService"]] = None

	def __new__(cls) -> Self:
		if cls._instance is None:
			cls._instance = super().__new__(cls)
		return cls._instance

	def __init__(self) -> None:
		if getattr(self, "_initialized", False):
			return

		self._database = DataBase()
		self._user_svc = UserService()
		self._submission_svc = SubmissionService()
		self._competition_svc = CompetitionService()
		self._teams: Dict[UUID, TeamRead] = dict()
		self._selected_team_id: Dict[UUID, UUID | None] = dict()  # Dict[user_id, team_id | None]
		self._user_teams: Dict[UUID, List[UUID]] = dict()
		self._memberships: MembershipDict = MembershipDict()

		self._initialized = True

	async def get_user_teams(self, user: UserRead) -> List[TeamRead]:
		if user.id in self._user_teams:
			return [await self.get_team(team_id) for team_id in self._user_teams.get(user.id)]

		self._user_teams[user.id] = []
		memberships = await self._database.get_memberships_by_user(user.id)
		for m in memberships:
			self._user_teams[user.id].append(m.team_id)
			self._memberships.upsert(m)

		return await self.get_user_teams(user)

	async def get_team(self, team_id: UUID) -> Optional[TeamRead]:
		if team_id in self._teams:
			return self._teams.get(team_id)

		self._teams[team_id] = await self._database.get_team(team_id)
		return await self.get_team(team_id)

	async def get_selected_team_by_user(self, user: UserRead) -> Optional[TeamRead]:
		if await self.has_selected_team(user):
			return await self.get_team(user.active_team_id)
		return None

	async def get_team_user(self, team_user_id: UUID) -> TeamUserRead | None:
		if team_user_id not in self._memberships:
			membership = await self._database.get_membership_by_id(team_user_id)
			if membership is None:
				return None
			self._memberships.upsert(membership)

		return self._memberships.get(team_user_id, None)

	async def get_user_by_team_user_id(self, team_user_id: UUID) -> UserRead | None:
		membership = await self.get_team_user(team_user_id)
		if membership is None:
			return None

		return await self._user_svc.get_user(membership.user_id)

	async def update_team(self, team: TeamUpdate) -> TeamRead:
		updated_team = await self._database.update_team(team)
		self._teams[team.id] = updated_team
		return updated_team

	async def create_team(self, team: TeamCreate) -> TeamRead:
		new_team = await self._database.create_team(team)
		self._teams[new_team.id] = new_team
		return new_team

	async def list_members(self, team_id: UUID) -> List[TeamUserRead]:
		memberships = await self._database.get_memberships_by_team(team_id)
		for membership in memberships:
			self._memberships.upsert(membership)
		return memberships

	async def team_member_count(self, team_id: UUID) -> int:
		return len(await self.list_members(team_id))

	async def is_user_in_team(self, team_id: UUID, user_id: UUID) -> bool:
		membership = await self._database.get_membership(user_id, team_id)
		if membership:
			self._memberships.upsert(membership)
		return membership is not None

	async def is_team_full(self, team: TeamRead) -> bool:
		if team.track_id is None:
			return True
		track = await self._competition_svc.get_track_by_id(team.track_id)
		if track is None:
			return True
		capacity = int(track.max_contestants)
		return await self.team_member_count(team.id) >= capacity

	async def is_competition_finished(self, team: TeamRead) -> bool:
		if team.track_id is None:
			return False
		track = await self._competition_svc.get_track_by_id(team.track_id)
		if track is None:
			return False
		competition = await self._competition_svc.get_competition_by_id(track.competition_id)
		if competition is None or competition.end_at is None:
			return False
		return competition.end_at <= datetime.utcnow()

	async def available_team_infos(self) -> List[Dict[str, Any]]:
		infos: List[Dict[str, Any]] = []
		page = 0
		page_size = 100
		now = datetime.utcnow()
		while True:
			teams_page, total = await self.list_teams_page(page=page, page_size=page_size)
			if not teams_page:
				break
			for team in teams_page:
				if team.track_id is None:
					continue
				track = await self._competition_svc.get_track_by_id(team.track_id)
				if track is None:
					continue
				competition = await self._competition_svc.get_competition_by_id(track.competition_id)
				if competition is None:
					continue
				if competition.end_at and competition.end_at <= now:
					continue
				capacity = int(track.max_contestants)
				member_count = await self.team_member_count(team.id)
				if member_count >= capacity:
					continue
				infos.append(
					{
						"team": team,
						"track": track,
						"competition": competition,
						"member_count": member_count,
						"capacity": capacity,
					}
				)
			if (page + 1) * page_size >= total:
				break
			page += 1
		infos.sort(key=lambda info: (info["team"].title.lower(), info["team"].slug.lower()))
		return infos

	async def list_teams_page(self, page: int, page_size: int) -> tuple[list[TeamRead], int]:
		limit = page_size
		offset = max(page, 0) * page_size
		items, total = await self._database.list_teams(limit=limit, offset=offset)
		for team in items:
			self._teams[team.id] = team
		return items, total

	async def upsert_team_user(self, team_user: TeamUserCreate | TeamUserUpdate) -> None:
		if (await self.get_team(team_user.team_id)) is None:
			raise ValueError("Team is not found")
		if (await self._user_svc.get_user(team_user.user_id)) is None:
			raise ValueError("User is not found")

		membership = await self._database.upsert_membership(team_user)
		self._memberships.upsert(membership)
		if membership:
			teams = self._user_teams.setdefault(membership.user_id, [])
			if membership.team_id not in teams:
				teams.append(membership.team_id)


	async def get_membership(self, user_id: UUID, team_id: UUID) -> Optional[TeamUserRead]:
		membership = self._memberships.get((user_id, team_id))
		if membership:
			return membership
		membership = await self._database.get_membership(user_id, team_id)
		self._memberships.upsert(membership)
		if membership:
			teams = self._user_teams.setdefault(membership.user_id, [])
			if membership.team_id not in teams:
				teams.append(membership.team_id)

		return membership

	async def can_team_submit(self, user: UserRead) -> bool:
		if not (await self.has_selected_team(user)):
			return False

		team = await self.get_selected_team_by_user(user)
		if team is None or team.track_id is None:
			return False
		track = await self._competition_svc.get_track_by_id(team.track_id)
		if track is None:
			return False
		max_count_submission = await self._competition_svc.max_count_submission(track)
		count_submission = await self._submission_svc.count_submissions(team)

		return count_submission < max_count_submission

	async def can_switch_team(self, user: UserRead) -> bool:
		teams = await self.get_user_teams(user)
		return len(teams) > 1

	async def has_selected_team(self, user: UserRead) -> bool:
		if user.active_team_id:
			team = await self.get_team(user.active_team_id)
			if team:
				if (user.id, user.active_team_id) not in self._memberships:
					self._memberships.upsert(await self._database.get_membership(user.id, user.active_team_id))
				return self._memberships.get((user.id, user.active_team_id)) is not None

		return False
