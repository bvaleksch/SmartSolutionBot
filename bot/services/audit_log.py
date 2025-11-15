# bot/services/audit_log.py
from __future__ import annotations

import inspect
import logging
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any, ClassVar, Iterable, Mapping, MutableMapping, Optional, Sequence, Self
from contextvars import ContextVar, Token

from smart_solution.db.database import DataBase
from smart_solution.db.schemas.audit_log import AuditLogCreate, AuditLogRead
from smart_solution.db.schemas.user import UserRead


class AuditLogService:
    """
    Centralised helper that stores every meaningful action in the ``audit_log`` table.

    The service normalises arbitrary payloads into JSON-friendly dictionaries, enriches
    them with call-site metadata and delegates persistence to :class:`smart_solution.db.database.DataBase`.
    """

    _instance: ClassVar[Optional["AuditLogService"]] = None

    def __new__(cls) -> "AuditLogService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return

        self._database = DataBase()
        self._logger = logging.getLogger("smart_solution.audit")
        self._module_name = Path(__file__).name
        self._initialized = True
        # per-update actor context (UUID of current user)
        self._actor_ctx: ContextVar[Optional[uuid.UUID]] = ContextVar("audit_actor", default=None)

    async def log(
        self,
        *,
        action: str,
        actor_id: uuid.UUID | None = None,
        payload: Any | None = None,
        include_context: bool = True,
    ) -> AuditLogRead:
        """
        Persist a low-level audit entry.

        :param action: short machine-readable label (``team.create``, ``submission.rate``…)
        :param actor_id: optional user identifier that initiated the action
        :param payload: arbitrary structure with details (will be serialised)
        :param include_context: whether to attach caller metadata automatically
        """
        payload_map = self._prepare_payload(payload)
        if include_context:
            payload_map.setdefault("_meta", {}).update(self._call_context())

        # fallback actor from context
        ctx_actor = self.current_actor()
        actor_id = actor_id if actor_id is not None else ctx_actor

        entry = await self._database.create_audit_log(
            AuditLogCreate(action=action, actor_id=actor_id, payload=payload_map)
        )
        self._logger.info(
            "AUDIT action=%s actor=%s entry=%s",
            action,
            str(actor_id) if actor_id else "-",
            entry.id,
        )
        return entry

    async def log_user_action(
        self,
        *,
        action: str,
        actor: UserRead | uuid.UUID | None,
        payload: Any | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> AuditLogRead:
        """
        Convenience helper for actions initiated by a Telegram user.

        Automatically stores user metadata alongside the payload.
        """
        actor_id = self._actor_id(actor)
        payload_map: MutableMapping[str, Any] = {}
        if payload is not None:
            payload_map["data"] = self._serialize(payload)
        if isinstance(actor, UserRead):
            payload_map["actor"] = {
                "id": str(actor.id),
                "role": actor.role,
                "username": actor.tg_username,
            }
        if extra:
            payload_map["extra"] = self._serialize(extra)
        return await self.log(action=action, actor_id=actor_id, payload=payload_map)

    async def list_entries(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
    ) -> tuple[list[AuditLogRead], int]:
        """Return recent audit entries."""
        return await self._database.list_audit_logs(
            limit=limit,
            offset=offset,
            actor_id=actor_id,
            action=action,
        )

    # --------------
    # Actor context
    # --------------
    def bind_actor(self, actor_id: Optional[uuid.UUID]) -> Token:
        return self._actor_ctx.set(actor_id)

    def unbind_actor(self, token: Token) -> None:
        try:
            self._actor_ctx.reset(token)
        except Exception:
            pass

    def current_actor(self) -> Optional[uuid.UUID]:
        try:
            return self._actor_ctx.get()
        except Exception:
            return None

    def _actor_id(self, actor: UserRead | uuid.UUID | None) -> uuid.UUID | None:
        if isinstance(actor, uuid.UUID):
            return actor
        if isinstance(actor, UserRead):
            return actor.id
        return None

    def _prepare_payload(self, payload: Any | None) -> dict[str, Any]:
        if payload is None:
            return {}
        serialized = self._serialize(payload)
        if isinstance(serialized, dict):
            return dict(serialized)
        return {"value": serialized}

    def serialize(self, value: Any) -> Any:
        """Public helper for shared serialization logic."""
        return self._serialize(value)

    def _serialize(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if is_dataclass(value):
            return {k: self._serialize(v) for k, v in asdict(value).items()}
        if isinstance(value, Mapping):
            return {str(k): self._serialize(v) for k, v in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [self._serialize(v) for v in value]
        if hasattr(value, "model_dump"):
            try:
                dumped = value.model_dump()
                return {k: self._serialize(v) for k, v in dumped.items()}
            except Exception:
                return str(value)
        if hasattr(value, "dict"):
            try:
                dumped = value.dict()  # type: ignore[call-arg]
                return {k: self._serialize(v) for k, v in dumped.items()}
            except Exception:
                return str(value)
        if hasattr(value, "__dict__"):
            return {
                k: self._serialize(v)
                for k, v in vars(value).items()
                if not k.startswith("_")
            }
        return str(value)

    def _call_context(self) -> dict[str, Any]:
        stack = inspect.stack()
        for frame in stack[2:]:
            path = Path(frame.filename)
            if path.name != self._module_name:
                return {
                    "module": path.stem,
                    "location": f"{path.name}:{frame.lineno}",
                    "function": frame.function,
                }
        return {}


audit_logger = AuditLogService()

def _resolve_actor(
    actor_fields: Iterable[str] | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    signature: inspect.Signature,
) -> UserRead | uuid.UUID | None:
    if not actor_fields:
        return None
    for field in actor_fields:
        if field in kwargs:
            candidate = kwargs[field]
            if candidate is not None:
                return candidate
    for idx, (name, _param) in enumerate(signature.parameters.items()):
        if name not in actor_fields:
            continue
        if idx < len(args):
            candidate = args[idx]
            if candidate is not None:
                return candidate
    return None


def _serialized_args(args: tuple[Any, ...], skip: int) -> list[Any]:
    if skip >= len(args):
        return []
    return [audit_logger.serialize(arg) for arg in args[skip:]]


def _serialized_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: audit_logger.serialize(v) for k, v in kwargs.items()}


async def _emit_action(
    *,
    action: str,
    actor: UserRead | uuid.UUID | None,
    payload: dict[str, Any],
) -> None:
    if actor is None:
        # fallback to context actor
        ctx = audit_logger.current_actor()
        actor = ctx
    if actor is not None:
        await audit_logger.log_user_action(action=action, actor=actor, payload=payload)
    else:
        await audit_logger.log(action=action, payload=payload)


def _wrap_async_callable(
    fn,
    action: str,
    *,
    skip_first_arg: bool,
    actor_fields: Iterable[str] | None,
):
    if getattr(fn, "__audit_wrapped__", False):
        return fn

    signature = inspect.signature(fn)
    skip_count = 1 if skip_first_arg else 0

    @wraps(fn)
    async def wrapper(*args, **kwargs):
        actor = _resolve_actor(actor_fields, args, kwargs, signature)
        if actor is None:
            actor = audit_logger.current_actor()
        payload = {
            "args": _serialized_args(args, skip_count),
            "kwargs": _serialized_kwargs(kwargs),
        }
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            payload["error"] = repr(exc)
            await _emit_action(action=f"{action}.error", actor=actor, payload=payload)
            raise
        payload["result"] = audit_logger.serialize(result)
        await _emit_action(action=action, actor=actor, payload=payload)
        return result

    wrapper.__audit_wrapped__ = True  # type: ignore[attr-defined]
    return wrapper


def instrument_service_class(
    cls,
    *,
    prefix: str | None = None,
    exclude: Iterable[str] | None = None,
    actor_fields: Iterable[str] | None = None,
) -> None:
    """Wrap async methods of a service class to emit audit entries."""
    action_prefix = prefix or cls.__name__
    excluded = set(exclude or [])

    for name, attr in list(cls.__dict__.items()):
        if name.startswith("_") or name in excluded:
            continue
        wrapped = None
        if inspect.iscoroutinefunction(attr):
            wrapped = _wrap_async_callable(
                attr,
                f"{action_prefix}.{name}",
                skip_first_arg=True,
                actor_fields=actor_fields,
            )
        elif isinstance(attr, staticmethod):
            func = attr.__func__
            if inspect.iscoroutinefunction(func):
                wrapped_func = _wrap_async_callable(
                    func,
                    f"{action_prefix}.{name}",
                    skip_first_arg=False,
                    actor_fields=actor_fields,
                )
                wrapped = staticmethod(wrapped_func)
        elif isinstance(attr, classmethod):
            func = attr.__func__
            if inspect.iscoroutinefunction(func):
                wrapped_func = _wrap_async_callable(
                    func,
                    f"{action_prefix}.{name}",
                    skip_first_arg=True,
                    actor_fields=actor_fields,
                )
                wrapped = classmethod(wrapped_func)

        if wrapped is not None:
            setattr(cls, name, wrapped)


def instrument_router_module(
    module_globals: dict[str, Any],
    *,
    prefix: str,
    actor_fields: Iterable[str] | None = ("current_user", "user", "actor"),
) -> None:
    """Wrap coroutine handlers in a router module for auditing."""
    for name, obj in list(module_globals.items()):
        if name.startswith("_"):
            continue
        if inspect.iscoroutinefunction(obj):
            module_globals[name] = _wrap_async_callable(
                obj,
                f"{prefix}.{name}",
                skip_first_arg=False,
                actor_fields=actor_fields,
            )


__all__ = [
    "AuditLogService",
    "audit_logger",
    "instrument_service_class",
    "instrument_router_module",
]
