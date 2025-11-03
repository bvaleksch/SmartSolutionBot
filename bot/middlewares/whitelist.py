# bot/middlewares/whitelist
from typing import Callable, Awaitable, Any, Dict
from aiogram import BaseMiddleware
from smart_solution.config import Settings

class WhitelistMiddleware(BaseMiddleware):
	async def __call__(self, 
		handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
		event: Any,
		data: Dict[str, Any]) -> Any:

		data["is_whitelisted"] = False
		user = data.get("current_user", None)
		if user and user.tg_username:
			data["is_whitelisted"] = user.tg_username in Settings().whitelist

		return await handler(event, data)

		