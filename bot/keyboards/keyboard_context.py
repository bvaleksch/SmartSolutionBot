# bot/keyboards/keyboard_context.py
from typing import Dict
from smart_solution.db.schemas.user import UserRead
from smart_solution.db.enums import UserRole
from smart_solution.bot.services.team import TeamService
from smart_solution.bot.services.language import LanguageService

class KeyboardContext:
	def __init__(self, user: UserRead) -> None:
		self.user = user
		self._initialized = False

	async def initialize(self) -> None:
		lng_svc = LanguageService()
		self.user_id = self.user.id
		self.role = self.user.role
		self.ui_mode = self.user.ui_mode
		self.lang_name = (await lng_svc.safe_autoget(self.user.preferred_language_id)).name

		# Contestant
		self.can_switch_team: bool = False
		self.has_selected_team: bool = False
		self.can_submit: bool = False
		if self.role == UserRole.CONTESTANT:
			await self.contestant_initialize()

		self._initialized = True

	async def admin_initialize(self) -> None:
		...

	async def contestant_initialize(self) -> None:
		team_svc = TeamService()
		self.can_switch_team = await team_svc.can_switch_team(self.user)
		self.has_selected_team = await team_svc.has_selected_team(self.user)
		self.can_submit = await team_svc.can_team_submit(self.user)

	async def unregistered_initialize(self) -> None:
		...

	async def myhash(self) -> int:
		if not self._initialized:
			await self.initialize()

		common_state = f"{self.role}{self.ui_mode}{self.lang_name}"
		contestant_state = f"{self.can_switch_team}{self.has_selected_team}{self.can_submit}"

		return hash(common_state + contestant_state)

