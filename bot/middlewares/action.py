# bot/middlewares/action.py
from typing import Dict, Any, Awaitable, Callable
from aiogram import BaseMiddleware
from aiogram.types import Message
from smart_solution.bot.services.action_registry import ActionRegistry

class ActionMiddleware(BaseMiddleware):
	def __init__(self) -> None:
		self.action_registry = ActionRegistry()

	async def __call__(self,
		handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
		event: Message,
		data: Dict[str, Any]) -> Any:
		text = event.text if event.text is not None else ""
		ui_mode = str(data["current_user"].ui_mode)
		role = str(data["current_user"].role)
		action = self.action_registry.resolve(text=text, ui_mode=ui_mode, role=role)
		data["ui_action"] = action
		return await handler(event, data)

