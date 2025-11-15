"""Notification service that informs users when a submission update occurs."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, ClassVar, Optional
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from smart_solution.db.schemas.submission import SubmissionRead, SubmissionUpdate
from smart_solution.db.schemas.user import UserRead
from smart_solution.db.enums import SubmissionStatus
from smart_solution.bot.routers.utils import get_localizer_by_user
from smart_solution.bot.services.audit_log import instrument_service_class

logger = logging.getLogger(__name__)


class SubmissionNotificationService:
	"""Singleton responsible for notifying contestants about submission updates."""

	_instance: ClassVar[Optional["SubmissionNotificationService"]] = None

	def __new__(cls) -> "SubmissionNotificationService":
		if cls._instance is None:
			cls._instance = super().__new__(cls)
		return cls._instance

	def __init__(self) -> None:
		if getattr(self, "_initialized", False):
			return
		self._initialized = True
		self._bot: Optional[Bot] = None

	def notify_update(self) -> Callable[[Callable[..., Awaitable[SubmissionRead]]], Callable[..., Awaitable[SubmissionRead]]]:
		"""Decorator that sends a message whenever ``update_submission`` succeeds."""

		def _decorator(func: Callable[..., Awaitable[SubmissionRead]]) -> Callable[..., Awaitable[SubmissionRead]]:
			async def _wrapper(service_self, *args, **kwargs):
				payload = self._extract_update_payload(args, kwargs)
				prev_state: Optional[SubmissionRead] = None
				if payload is not None:
					prev_state = await self._safe_snapshot(service_self, payload.id)

				updated = await func(service_self, *args, **kwargs)

				if payload is not None and isinstance(updated, SubmissionRead):
					try:
						await self._handle_notification(service_self, updated, prev_state)
					except Exception:
						# Notification failures should not block submission updates.
						logger.exception(
							"Failed to send submission update notification for %s",
							getattr(payload, "id", None),
						)
						return updated

				return updated

			return _wrapper

		return _decorator

	@staticmethod
	def _extract_update_payload(args, kwargs) -> Optional[SubmissionUpdate]:
		for arg in args:
			if isinstance(arg, SubmissionUpdate):
				return arg
		for value in kwargs.values():
			if isinstance(value, SubmissionUpdate):
				return value
		return None

	@staticmethod
	async def _safe_snapshot(service_self, submission_id: UUID) -> Optional[SubmissionRead]:
		try:
			return await service_self.get_submission(submission_id)
		except Exception:
			logger.debug("Could not snapshot submission %s before update", submission_id, exc_info=True)
			return None

	async def _handle_notification(
		self,
		service_self,
		updated: SubmissionRead,
		previous: Optional[SubmissionRead],
	) -> None:
		"""Build and send a localized notification if anything meaningful changed."""
		if previous is not None:
			if (
				previous.status == updated.status
				and previous.value == updated.value
				and previous.title == updated.title
			):
				logger.debug("Submission %s unchanged; skipping notification", updated.id)
				return

		user = await service_self.get_user_by_submission(updated)
		if not isinstance(user, UserRead):
			logger.debug("Submission %s: user lookup failed; skipping notification", updated.id)
			return

		if not getattr(user, "tg_id", None):
			logger.debug(
				"Submission %s: user %s has no tg_id; skipping notification",
				updated.id,
				getattr(user, "id", None),
			)
			return

		lz = await get_localizer_by_user(user)
		status_current = self._status_label(lz, updated.status)
		value_current = self._format_value(updated.value)

		lines = [
			lz.get("team_user.submit.notify.header"),
			lz.get("team_user.submit.notify.title", title=updated.title),
		]

		if previous is not None and previous.status != updated.status:
			status_prev = self._status_label(lz, previous.status)
			lines.append(
				lz.get("team_user.submit.notify.status_change", old=status_prev, new=status_current)
			)
		else:
			lines.append(lz.get("team_user.submit.notify.status", value=status_current))

		if previous is not None and previous.value != updated.value:
			value_prev = self._format_value(previous.value)
			lines.append(
				lz.get("team_user.submit.notify.value_change", old=value_prev, new=value_current)
			)
		else:
			lines.append(lz.get("team_user.submit.notify.value", value=value_current))

		text = "\n".join(lines)
		await self._send_message(user, text)
		logger.info(
			"Submission %s notification sent to user %s",
			updated.id,
			getattr(user, "id", None),
		)

	@staticmethod
	def _status_label(lz, status: SubmissionStatus | str | None) -> str:
		if isinstance(status, SubmissionStatus):
			code = status.value.lower()
		else:
			code = str(status or "").lower()
		if not code:
			return "—"
		try:
			return lz.get(f"submissions.status.{code}")
		except KeyError:
			return code.upper()

	@staticmethod
	def _format_value(value: Optional[float]) -> str:
		if value is None:
			return "—"
		return f"{value:.4f}".rstrip("0").rstrip(".") or "0"

	async def _send_message(self, user: UserRead, text: str) -> None:
		"""Send a Telegram message to the submission owner, handling delivery errors."""
		bot = self._current_bot()
		if bot is None:
			logger.debug("No active bot instance; cannot notify user %s", getattr(user, "id", None))
			return

		chat_id = getattr(user, "tg_id", None)
		if not isinstance(chat_id, int):
			logger.debug("User %s has invalid tg_id; skipping notification", getattr(user, "id", None))
			return

		try:
			await bot.send_message(chat_id=chat_id, text=text)
		except (TelegramForbiddenError, TelegramBadRequest):
			# Ignore delivery problems (user blocked bot, etc.).
			logger.warning(
				"Failed to deliver submission notification to user %s",
				getattr(user, "id", None),
				exc_info=True,
			)
			return

	def _current_bot(self) -> Optional[Bot]:
		if self._bot is not None:
			return self._bot
		logger.debug("Submission notifier bot is not bound")
		return None

	def bind_bot(self, bot: Bot) -> None:
		"""Provide the active bot instance so messages can be delivered."""
		self._bot = bot
		logger.info("Submission notifier bound to bot %s", getattr(bot, "id", None))


instrument_service_class(SubmissionNotificationService, prefix="services.submission_notify", exclude={"notify_update"})


submission_notifier = SubmissionNotificationService()
