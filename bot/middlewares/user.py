# bot/middlewares/user.py
from uuid import UUID
from typing import Callable, Self, Awaitable, Any, ClassVar, Optional, Dict
from aiogram import BaseMiddleware
from aiogram.types import User as TgUser
from smart_solution.db.schemas.user import UserRead, UserCreate, UserUpdate
from smart_solution.bot.services.user import UserService
from smart_solution.db.enums import UiMode

class UserMiddleware(BaseMiddleware):
	_instance: ClassVar[Optional["UserMiddleware"]] = None

	def __new__(cls, *args, **kwargs) -> Self:
		if cls._instance is None:
			cls._instance = super().__new__(cls)

		return cls._instance

	def __init__(self,  *args, **kwargs) -> None:
		if getattr(self, "_initialized", False):
			return

		self._user_service = UserService()

		self._initialized = True
		
	async def get_user(self, tg_user: TgUser) -> Optional[UserRead]:
		return await self._user_service.get_user(tg_id=tg_user.id, tg_username=tg_user.username if tg_user.username is not None else ..., autocreate=True)

	async def __call__(self,
		handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
		event: Any,
		data: dict[str, Any]) -> Any:
		tg_user: Optional[TgUser] = data.get("event_from_user")
		if tg_user is None:
			return

		user = await self.get_user(tg_user)
		if user is None:
			return

		data["current_user"] = user
		return await handler(event, data)

