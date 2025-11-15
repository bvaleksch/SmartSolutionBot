# bot/middlewares/rate_limit.py
from time import time
from uuid import UUID
from typing import Callable, Awaitable, Any, Dict, List, Self, ClassVar, Optional
from aiogram import BaseMiddleware
from smart_solution.config import Settings
from smart_solution.bot.services.audit_log import instrument_service_class

class RateLimitMiddleware(BaseMiddleware):
	"""
	In-memory rate limiter for aiogram v3.

	This middleware tracks per-user events in a sliding time window and:
	  1) denies handling when the number of recent events exceeds `max_requests` (soft cap);
	  2) bans a user when the number of recent events reaches `ban_threshold`
		 (hard cap) and keeps the ban for `ban_duration_seconds`.

	Design:
	  - State is kept in process memory:
		  * `records[user_id] -> list[float]` of UNIX timestamps (seconds);
		  * `banned_until[user_id] -> float` UNIX timestamp when the ban ends.
	  - The window length is `window` seconds (sliding window).
	  - All limits are configured via `Settings()`.

	Notes:
	  - This implementation does not persist data; restart clears limits.
	  - It is not multi-process safe; if you run several bot workers, you need
		a shared store (e.g., Redis) instead of local dicts.
	  - The middleware relies on `current_user` being present in `data` (e.g.,
		provided by an AuthMiddleware) and uses `current_user.id` as the key.
	"""
	_instance: ClassVar[Optional["RateLimitMiddleware"]] = None

	def __new__(cls, *args, **kwargs) -> Self:
		"""
		Singleton allocator.

		Returns:
			A single shared instance of RateLimitMiddleware across the process.

		Rationale:
			Avoid multiple independent in-memory buckets/bans if the class is
			instantiated more than once (e.g., during app wiring).
		"""
		if cls._instance is None:
			cls._instance = super().__new__(cls)
		return cls._instance

	def __init__(self) -> None:
		"""
		Initialize configuration and in-memory storages.

		Settings read:
			- Settings.max_requests: int
				Maximum allowed events within the sliding window (soft cap).
			- Settings.ban_threshold: int
				Events within the window after which a ban is applied (hard cap).
			- Settings.ban_duration_seconds: int
				Ban duration in seconds.
			- Settings.period: int
				Sliding window length in seconds (stored as `self.window`).

		Internal state:
			- self.records: Dict[UUID, List[float]]
				Per-user timestamps (seconds since epoch).
			- self.banned_until: Dict[UUID, float]
				Per-user ban end timestamps (seconds since epoch).

		Idempotent:
			Guarded by `_initialized` so repeated constructions are cheap.
		"""
		if getattr(self, "_initialized", False):
			return 

		s = Settings()
		self.max_requests: int = s.max_requests
		self.ban_threshold: int = s.ban_threshold
		self.ban_duration_seconds: int = s.ban_duration_seconds
		self.window: int = s.period
		self.banned_until: Dict[UUID, float] = dict()
		self.records: Dict[UUID, List[float]] = dict()

		self._initialized = True

	def unban_user(self, uuid: UUID) -> None:
		"""
		Remove a user from the ban map.

		Args:
			uuid: User identifier (UUID) used as the in-memory key.

		Behavior:
			- If the user is present in `banned_until`, the entry is removed.
			- No-op if the user is not banned.
		"""
		if uuid in self.banned_until:
			self.banned_until.pop(uuid)

	def auto_unban_users(self) -> None:
		"""
		Garbage-collect expired bans.

		Compares the current time with each stored `banned_until` value and
		removes users whose ban has already elapsed.

		Complexity:
			O(N) over the number of currently banned users.
		"""
		current_time = time()
		for user_id in self.banned_until.keys():
			if self.banned_until[user_id] >= current_time:
				self.unban_user(user_id)

	def create_record_and_maybe_ban(self, user_id: UUID) -> None:
		"""
		Append a new event for the user, evict old timestamps, and apply/extend ban if needed.

		Steps:
			1) Append current timestamp to `records[user_id]`;
			2) Evict head items older than `window` seconds (sliding window);
			3) If number of timestamps in the window meets/exceeds `ban_threshold`,
			   set (or extend) a ban in `banned_until[user_id]`.

		Args:
			user_id: User UUID key.

		Notes:
			- Called on every update after `auto_unban_users()`.
			- This method is responsible for both accounting and ban decisions.
		"""
		current_time = time()
		if user_id not in self.records:
			self.records[user_id] = list()
		self.records[user_id].append(current_time)

		while ((current_time - self.records[user_id][0]) > self.window):
			self.records[user_id].pop(0)
		
		cnt = len(self.records[user_id])
		if (cnt > self.ban_threshold):
			self.banned_until.update(user_id, current_time + self.ban_duration_seconds)

	def is_user_banned(self, user_id):
		"""
		Check whether a user is currently banned.

		Args:
			user_id: User UUID key.

		Returns:
			True if the user is present in `banned_until` and the ban has not expired yet;
			otherwise False.
		"""
		return user_id in self.banned_until

	async def __call__(self,
		handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
		event: Any,
		data: Dict[str, Any]) -> Any:
		"""
		aiogram middleware entrypoint.

		Flow:
			1) If `current_user` is missing in `data`, skip rate limiting.
			2) Call `auto_unban_users()` to drop expired bans.
			3) Call `create_record_and_maybe_ban(user.id)` to record the event.
			4) If user is banned, short-circuit (deny handling).
			5) If soft cap `max_requests` is exceeded, short-circuit (deny).
			6) Otherwise, pass control to the next handler.

		Expectations:
			- `data["current_user"]` is an object with `.id: UUID`.

		Returns:
			The result of the next handler if allowed; otherwise None (message is ignored).
		"""
		user = data.get("current_user", None)
		if user is None:
			return await handler(event, data)

		self.auto_unban_users()
		self.create_record_and_maybe_ban(user.id)
		if self.is_user_banned(user.id) or (len(self.records[user.id]) > self.max_requests):
			return

		return await handler(event, data)


instrument_service_class(RateLimitMiddleware, prefix="middleware.rate_limit", actor_fields=("current_user",))
