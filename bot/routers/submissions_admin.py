# bot/routers/submissions_admin.py
from __future__ import annotations

import asyncio
from datetime import datetime
import uuid
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.types import FSInputFile, BufferedInputFile
from aiogram.exceptions import TelegramRetryAfter

from smart_solution.config import Settings
from smart_solution.db.enums import UiMode, UserRole, SubmissionStatus
from smart_solution.db.schemas.user import UserRead
from smart_solution.db.schemas.submission import SubmissionRead, SubmissionUpdate
from smart_solution.bot.filters.action_like import ActionLike
from smart_solution.bot.keyboards.user_keyboard_factory import UserKeyboardFactory
from smart_solution.bot.routers.utils import get_localizer_by_user
from smart_solution.bot.services.user import UserService
from smart_solution.bot.services.submission import SubmissionService
from smart_solution.bot.services.team import TeamService

router = Router(name="submissions_admin")

PAGE_SIZE = 8
DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
FILE_PART_LIMIT_BYTES = max(1_048_576, Settings().submission_file_part_max_bytes)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC_TZ = ZoneInfo("UTC")
MOSCOW_LABEL = "MSK"

VIEW_PREFIX = "sadm.view"
RATE_PREFIX = "sadm.rate"
RERATE_PREFIX = "sadm.rerate"
STATUS_PREFIX = "sadm.status"
DOWNLOAD_PREFIX = "sadm.dl"


class SubmissionModerationFSM(StatesGroup):
	waiting_selection = State()
	waiting_value = State()
	waiting_status = State()


def _is_admin(user: UserRead) -> bool:
	return str(user.role or "").lower() == UserRole.ADMIN.value


def _status_label(lz, status: SubmissionStatus | str) -> str:
	code = status.value if isinstance(status, SubmissionStatus) else str(status)
	code = code.lower()
	try:
		return lz.get(f"submissions.status.{code}")
	except KeyError:
		return code.upper()


def _ensure_utc(dt: datetime) -> datetime:
	if dt.tzinfo is None:
		return dt.replace(tzinfo=UTC_TZ)
	return dt.astimezone(UTC_TZ)


def _format_datetime_moscow(dt: datetime) -> str:
	local = _ensure_utc(dt).astimezone(MOSCOW_TZ)
	return f"{local.strftime('%Y-%m-%d %H:%M')} {MOSCOW_LABEL}"


def _format_value(value: Optional[float]) -> str:
	if value is None:
		return "—"
	return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _format_bytes(num_bytes: int) -> str:
	size = float(max(num_bytes, 0))
	units = ["B", "KB", "MB", "GB", "TB"]
	for unit in units:
		if size < 1024.0 or unit == units[-1]:
			if unit == "B":
				return f"{int(size)} {unit}"
			return f"{size:.1f} {unit}"
		size /= 1024.0
	return f"{size:.1f} TB"


def _format_user_label(user) -> str:
	if user is None:
		return "—"
	parts = []
	if user.first_name:
		parts.append(user.first_name)
		parts.append(user.last_name)
	full_name = " ".join(parts).strip()
	if user.tg_username:
		tag = f"@{user.tg_username}"
		return f"{full_name} ({tag})" if full_name else tag
	return full_name or "—"


def _render_submission_details(
	submission: SubmissionRead,
	team,
	user,
	lz,
) -> str:
	created = _format_datetime_moscow(submission.created_at)
	lines = [
		lz.get("submissions.detail.title", submission_id=str(submission.id), title=submission.title),
		lz.get("submissions.detail.team", team=team.title if team else lz.get("submissions.detail.unknown")),
		lz.get("submissions.detail.user", user=_format_user_label(user)),
		lz.get("submissions.detail.status", status=_status_label(lz, submission.status)),
			lz.get("submissions.detail.value", value=_format_value(submission.value)),
		lz.get("submissions.detail.created_at", created=created),
	]
	return "\n".join(lines)


def _build_status_keyboard(lz, mode: str) -> InlineKeyboardMarkup:
	rows: list[list[InlineKeyboardButton]] = [
		[
			InlineKeyboardButton(
				text=lz.get("submissions.actions.accept"),
				callback_data=f"{STATUS_PREFIX}:accepted",
			)
		],
		[
			InlineKeyboardButton(
				text=lz.get("submissions.actions.reject"),
				callback_data=f"{STATUS_PREFIX}:rejected",
			)
		],
		[
			InlineKeyboardButton(
				text=lz.get("submissions.actions.error"),
				callback_data=f"{STATUS_PREFIX}:error",
			)
		],
		[
			InlineKeyboardButton(
				text=lz.get("submissions.actions.cancel"),
				callback_data=f"{STATUS_PREFIX}:cancel",
			)
		],
	]
	return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_submission_list(
	target: Message | CallbackQuery,
	lz,
	status_filter: SubmissionStatus | None,
	page: int,
	prefix: str,
	header_key: str,
	empty_key: str,
) -> tuple[bool, int]:
	sub_svc = SubmissionService()
	requested_page = max(0, page)
	items, total = await sub_svc.list_submissions_page(requested_page, PAGE_SIZE, status_filter)

	if total == 0 or not items:
		text = lz.get(empty_key)
		if isinstance(target, Message):
			await target.answer(text)
		else:
			await target.answer(text, show_alert=True)
		return False, requested_page

	pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
	current_page = min(requested_page, pages - 1)
	if current_page != requested_page:
		items, _ = await sub_svc.list_submissions_page(current_page, PAGE_SIZE, status_filter)

	def _list_buttons(submissions: Sequence[SubmissionRead]) -> InlineKeyboardMarkup:
		rows: list[list[InlineKeyboardButton]] = []
		for submission in submissions:
			created = _format_datetime_moscow(submission.created_at)
			status_text = _status_label(lz, submission.status)
			value_text = _format_value(submission.value)
			button_text = lz.get(
				"submissions.list.item",
				title=submission.title,
				status=status_text,
				created=created,
				value=value_text,
			)
			callback = f"{prefix}.pick:{current_page}:{submission.id}"
			rows.append([InlineKeyboardButton(text=button_text, callback_data=callback)])

		nav: list[InlineKeyboardButton] = []
		if current_page > 0:
			nav.append(
				InlineKeyboardButton(
					text=lz.get("submissions.list.prev"),
					callback_data=f"{prefix}.page:{current_page-1}",
				)
			)
		if current_page + 1 < pages:
			nav.append(
				InlineKeyboardButton(
					text=lz.get("submissions.list.next"),
					callback_data=f"{prefix}.page:{current_page+1}",
				)
			)
		if nav:
			rows.append(nav)
		rows.append(
			[
				InlineKeyboardButton(
					text=lz.get("submissions.list.close"),
					callback_data=f"{prefix}.cancel",
				)
			]
		)
		return InlineKeyboardMarkup(inline_keyboard=rows)

	header = lz.get(
		header_key,
		page=str(current_page + 1),
		pages=str(pages),
		total=str(total),
	)
	keyboard = _list_buttons(items)

	if isinstance(target, Message):
		await target.answer(header, reply_markup=keyboard)
	else:
		await target.message.edit_text(header, reply_markup=keyboard)
		await target.answer()

	return True, current_page


async def _open_submission_details(
	cq: CallbackQuery,
	submission_id: uuid.UUID,
	page: int,
	lz,
) -> None:
	sub_svc = SubmissionService()
	team_svc = TeamService()
	user_svc = UserService()

	submission = await sub_svc.get_submission(submission_id)
	membership = await team_svc.get_team_user(submission.team_user_id)
	team = await team_svc.get_team(membership.team_id) if membership else None
	user = await user_svc.get_user(uid=membership.user_id, autoupdate=False) if membership else None

	text = _render_submission_details(submission, team, user, lz)

	rows: list[list[InlineKeyboardButton]] = []
	rows.append(
		[
			InlineKeyboardButton(
				text=lz.get("submissions.detail.download"),
				callback_data=f"{DOWNLOAD_PREFIX}:{submission.id}",
			)
		]
	)
	rows.append(
		[
			InlineKeyboardButton(
				text=lz.get("submissions.list.back"),
				callback_data=f"{VIEW_PREFIX}.back:{page}",
			)
		]
	)

	keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
	await cq.message.edit_text(text, reply_markup=keyboard)
	await cq.answer()


def _submission_file_path(submission: SubmissionRead) -> Path:
	path = (DATA_ROOT / submission.file_path).resolve()
	try:
		path.relative_to(DATA_ROOT)
	except ValueError:
		raise FileNotFoundError("Submission path points outside data directory.")
	return path


async def _send_submission_file(message: Message, path: Path, lz, submission_id: uuid.UUID, title: str) -> None:
	chunk_size = max(1, FILE_PART_LIMIT_BYTES)
	try:
		total_size = path.stat().st_size
	except OSError:
		total_size = 0

	if total_size <= chunk_size:
		file = FSInputFile(path, filename=path.name)
		await message.answer_document(
			file,
			caption=lz.get("submissions.detail.file_caption", submission_id=str(submission_id), title=title),
		)
		return

	total_parts = max(1, (total_size + chunk_size - 1) // chunk_size)
	width = len(str(total_parts))
	first_part = f"{path.name}.part{1:0{width}d}"
	last_part = f"{path.name}.part{total_parts:0{width}d}"
	await message.answer(
		lz.get(
			"submissions.detail.chunk_start",
			size=_format_bytes(total_size),
			limit=_format_bytes(chunk_size),
			parts=str(total_parts),
			first=first_part,
			last=last_part,
		)
	)
	await _send_document_parts(message, path, total_parts, width, chunk_size, lz)


async def _send_document_parts(
	message: Message,
	path: Path,
	total_parts: int,
	width: int,
	chunk_size: int,
	lz,
) -> None:
	base_name = path.name
	with path.open("rb") as src:
		for idx in range(1, total_parts + 1):
			chunk = src.read(chunk_size)
			if not chunk:
				break
			filename = f"{base_name}.part{idx:0{width}d}"
			caption = None
			if idx == 1:
				caption = lz.get(
					"submissions.detail.chunk_caption",
					index=str(idx),
					total=str(total_parts),
					base=base_name,
				)
			await _deliver_chunk(message, chunk, filename, caption)


async def _deliver_chunk(message: Message, data: bytes, filename: str, caption: str | None) -> None:
	for attempt in range(3):
		try:
			await message.answer_document(
				BufferedInputFile(data, filename=filename),
				caption=caption,
			)
			return
		except TelegramRetryAfter as exc:
			delay = getattr(exc, "retry_after", 2) or 2
			await asyncio.sleep(max(1, int(delay)))
	# give up with standard exception
	await message.answer_document(
		BufferedInputFile(data, filename=filename),
		caption=caption,
	)


@router.message(ActionLike("buttons.submission:home:admin"))
async def submissions_mode_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
	if not _is_admin(current_user):
		return
	await state.clear()
	lz = await get_localizer_by_user(current_user)
	user_svc = UserService()
	current_user = await user_svc.change_ui_mode(current_user, UiMode.SUBMISSION)
	keyboard = await UserKeyboardFactory().build_for_user(current_user)
	await message.answer(lz.get("submissions.mode.enter"), reply_markup=keyboard)


@router.message(ActionLike("buttons.back:submission:admin"))
async def submissions_mode_back(message: Message, current_user: UserRead, state: FSMContext) -> None:
	if not _is_admin(current_user):
		return
	await state.clear()
	lz = await get_localizer_by_user(current_user)
	user_svc = UserService()
	current_user = await user_svc.change_ui_mode(current_user, UiMode.HOME)
	keyboard = await UserKeyboardFactory().build_for_user(current_user)
	await message.answer(lz.get("mode.home"), reply_markup=keyboard)


@router.message(ActionLike("buttons.view_submission:submission:admin"))
async def submissions_view_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
	if not _is_admin(current_user):
		return
	await state.clear()
	lz = await get_localizer_by_user(current_user)
	await _send_submission_list(
		message,
		lz,
		status_filter=None,
		page=0,
		prefix=VIEW_PREFIX,
		header_key="submissions.view.list_title",
		empty_key="submissions.view.empty",
	)


@router.callback_query(F.data.startswith(f"{VIEW_PREFIX}.page:"))
async def submissions_view_page(cq: CallbackQuery, current_user: UserRead) -> None:
	if not _is_admin(current_user):
		return
	lz = await get_localizer_by_user(current_user)
	page = int(cq.data.split(":")[1])
	await _send_submission_list(
		cq,
		lz,
		status_filter=None,
		page=page,
		prefix=VIEW_PREFIX,
		header_key="submissions.view.list_title",
		empty_key="submissions.view.empty",
	)


@router.callback_query(F.data.startswith(f"{VIEW_PREFIX}.pick:"))
async def submissions_view_pick(cq: CallbackQuery, current_user: UserRead) -> None:
	if not _is_admin(current_user):
		return
	_, payload = cq.data.split(f"{VIEW_PREFIX}.pick:", maxsplit=1)
	page_str, submission_id_str = payload.split(":", maxsplit=1)
	page = int(page_str)
	submission_id = uuid.UUID(submission_id_str)
	lz = await get_localizer_by_user(current_user)
	await _open_submission_details(cq, submission_id, page, lz)


@router.callback_query(F.data.startswith(f"{VIEW_PREFIX}.back:"))
async def submissions_view_back(cq: CallbackQuery, current_user: UserRead) -> None:
	if not _is_admin(current_user):
		return
	page = int(cq.data.split(":")[1])
	lz = await get_localizer_by_user(current_user)
	await _send_submission_list(
		cq,
		lz,
		status_filter=None,
		page=page,
		prefix=VIEW_PREFIX,
		header_key="submissions.view.list_title",
		empty_key="submissions.view.empty",
	)


@router.callback_query(F.data == f"{VIEW_PREFIX}.cancel")
async def submissions_view_cancel(cq: CallbackQuery, current_user: UserRead) -> None:
	if not _is_admin(current_user):
		return
	await cq.message.edit_reply_markup(reply_markup=None)
	await cq.answer()


@router.callback_query(F.data.startswith(f"{DOWNLOAD_PREFIX}:"))
async def submissions_download(cq: CallbackQuery, current_user: UserRead) -> None:
	if not _is_admin(current_user):
		return
	submission_id = uuid.UUID(cq.data.split(":")[1])
	lz = await get_localizer_by_user(current_user)
	sub_svc = SubmissionService()
	try:
		submission = await sub_svc.get_submission(submission_id)
		path = _submission_file_path(submission)
	except FileNotFoundError:
		await cq.answer(lz.get("submissions.detail.file_missing"), show_alert=True)
		return
	except Exception:
		await cq.answer(lz.get("submissions.detail.not_found"), show_alert=True)
		return

	await _send_submission_file(
		cq.message,
		path,
		lz,
		submission_id=submission_id,
		title=submission.title,
	)
	await cq.answer()


async def _start_moderation_flow(
	message: Message,
	state: FSMContext,
	current_user: UserRead,
	status_filter: SubmissionStatus,
	mode: str,
	prefix: str,
	header_key: str,
	empty_key: str,
) -> None:
	lz = await get_localizer_by_user(current_user)
	has_items, page = await _send_submission_list(
		message,
		lz,
		status_filter=status_filter,
		page=0,
		prefix=prefix,
		header_key=header_key,
		empty_key=empty_key,
	)
	if not has_items:
		await state.clear()
		return

	await state.update_data(
		mode=mode,
		status_filter=status_filter.value,
		prefix=prefix,
		header_key=header_key,
		empty_key=empty_key,
		page=page,
	)
	await state.set_state(SubmissionModerationFSM.waiting_selection)


@router.message(ActionLike("buttons.rate_submission:submission:admin"))
async def submissions_rate_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
	if not _is_admin(current_user):
		return
	await state.clear()
	await _start_moderation_flow(
		message,
		state,
		current_user,
		status_filter=SubmissionStatus.PENDING,
		mode="rate",
		prefix=RATE_PREFIX,
		header_key="submissions.rate.list_title",
		empty_key="submissions.rate.empty",
	)


@router.message(ActionLike("buttons.rerate_submission:submission:admin"))
async def submissions_rerate_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
	if not _is_admin(current_user):
		return
	await state.clear()
	await _start_moderation_flow(
		message,
		state,
		current_user,
		status_filter=SubmissionStatus.ACCEPTED,
		mode="rerate",
		prefix=RERATE_PREFIX,
		header_key="submissions.rerate.list_title",
		empty_key="submissions.rerate.empty",
	)


async def _moderation_page_callback(
	cq: CallbackQuery,
	current_user: UserRead,
	state: FSMContext,
	status_filter: SubmissionStatus,
	prefix: str,
	header_key: str,
	empty_key: str,
) -> None:
	if not _is_admin(current_user):
		return
	page = int(cq.data.split(":")[1])
	lz = await get_localizer_by_user(current_user)
	has_items, actual_page = await _send_submission_list(
		cq,
		lz,
		status_filter=status_filter,
		page=page,
		prefix=prefix,
		header_key=header_key,
		empty_key=empty_key,
	)
	if has_items:
		await state.update_data(page=actual_page)
	else:
		await state.clear()


@router.callback_query(SubmissionModerationFSM.waiting_selection, F.data.startswith(f"{RATE_PREFIX}.page:"))
async def submissions_rate_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
	await _moderation_page_callback(
		cq,
		current_user,
		state,
		status_filter=SubmissionStatus.PENDING,
		prefix=RATE_PREFIX,
		header_key="submissions.rate.list_title",
		empty_key="submissions.rate.empty",
	)


@router.callback_query(SubmissionModerationFSM.waiting_selection, F.data.startswith(f"{RERATE_PREFIX}.page:"))
async def submissions_rerate_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
	await _moderation_page_callback(
		cq,
		current_user,
		state,
		status_filter=SubmissionStatus.ACCEPTED,
		prefix=RERATE_PREFIX,
		header_key="submissions.rerate.list_title",
		empty_key="submissions.rerate.empty",
	)


async def _handle_moderation_pick(
	cq: CallbackQuery,
	current_user: UserRead,
	state: FSMContext,
	expected_status: SubmissionStatus,
) -> None:
	if not _is_admin(current_user):
		return
	data = await state.get_data()
	mode = data.get("mode")
	if mode not in {"rate", "rerate"}:
		await cq.answer()
		return

	_, payload = cq.data.split(":", maxsplit=1)
	page_str, submission_id_str = payload.split(":", maxsplit=1)
	submission_id = uuid.UUID(submission_id_str)
	page = int(page_str)

	lz = await get_localizer_by_user(current_user)
	sub_svc = SubmissionService()
	submission = await sub_svc.get_submission(submission_id)
	if submission.status != expected_status:
		await cq.answer(lz.get("submissions.rate.outdated"), show_alert=True)
		has_items, actual_page = await _send_submission_list(
			cq,
			lz,
			status_filter=expected_status,
			page=page,
			prefix=data.get("prefix", RATE_PREFIX),
			header_key=data.get("header_key", "submissions.rate.list_title"),
			empty_key=data.get("empty_key", "submissions.rate.empty"),
		)
		if has_items:
			await state.update_data(page=actual_page)
		else:
			await state.clear()
		return

	team_svc = TeamService()
	user_svc = UserService()
	membership = await team_svc.get_team_user(submission.team_user_id)
	team = await team_svc.get_team(membership.team_id) if membership else None
	user = await user_svc.get_user(uid=membership.user_id, autoupdate=False) if membership else None

	details = _render_submission_details(submission, team, user, lz)
	await cq.message.edit_text(details)
	await cq.answer()

	await state.update_data(
		submission_id=str(submission_id),
		page=page,
		value_input=None,
	)
	await state.set_state(SubmissionModerationFSM.waiting_value)

	await cq.message.answer(lz.get("submissions.rate.ask_value"))


@router.callback_query(SubmissionModerationFSM.waiting_selection, F.data.startswith(f"{RATE_PREFIX}.pick:"))
async def submissions_rate_pick(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
	await _handle_moderation_pick(cq, current_user, state, SubmissionStatus.PENDING)


@router.callback_query(SubmissionModerationFSM.waiting_selection, F.data.startswith(f"{RERATE_PREFIX}.pick:"))
async def submissions_rerate_pick(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
	await _handle_moderation_pick(cq, current_user, state, SubmissionStatus.ACCEPTED)


@router.callback_query(SubmissionModerationFSM.waiting_selection, (F.data == f"{RATE_PREFIX}.cancel") | (F.data == f"{RERATE_PREFIX}.cancel"))
async def submissions_moderation_cancel(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
	if not _is_admin(current_user):
		return
	await state.clear()
	await cq.message.edit_reply_markup(reply_markup=None)
	lz = await get_localizer_by_user(current_user)
	await cq.answer(lz.get("submissions.rate.cancelled"), show_alert=False)


@router.message(SubmissionModerationFSM.waiting_value)
async def submissions_moderation_value(message: Message, current_user: UserRead, state: FSMContext) -> None:
	if not _is_admin(current_user):
		return
	data = await state.get_data()
	mode = data.get("mode")
	if mode not in {"rate", "rerate"}:
		await state.clear()
		return

	text = (message.text or "").strip()
	lz = await get_localizer_by_user(current_user)
	value_input: Optional[float]

	if text.lower() in {"skip", "-"}:
		value_input = None
	else:
		try:
			value_input = float(text.replace(",", "."))
		except ValueError:
			await message.answer(lz.get("submissions.rate.invalid_value"))
			return

	await state.update_data(value_input=value_input)
	await state.set_state(SubmissionModerationFSM.waiting_status)
	await message.answer(
		lz.get("submissions.rate.choose_status"),
		reply_markup=_build_status_keyboard(lz, mode),
	)


@router.callback_query(SubmissionModerationFSM.waiting_status, F.data == f"{STATUS_PREFIX}:cancel")
async def submissions_status_cancel(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
	if not _is_admin(current_user):
		return
	await state.clear()
	await cq.message.edit_reply_markup(reply_markup=None)
	lz = await get_localizer_by_user(current_user)
	await cq.answer(lz.get("submissions.rate.cancelled"))


@router.callback_query(SubmissionModerationFSM.waiting_status, F.data.startswith(f"{STATUS_PREFIX}:"))
async def submissions_status_apply(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
	if not _is_admin(current_user):
		return
	status_code = cq.data.split(":")[1]
	if status_code == "cancel":
		return

	data = await state.get_data()
	mode = data.get("mode")
	if mode not in {"rate", "rerate"}:
		await state.clear()
		await cq.answer()
		return

	try:
		new_status = SubmissionStatus(status_code)
	except ValueError:
		await cq.answer()
		return

	submission_id = uuid.UUID(data["submission_id"])
	value_input = data.get("value_input")

	sub_svc = SubmissionService()
	submission = await sub_svc.get_submission(submission_id)

	if mode == "rate" and submission.status != SubmissionStatus.PENDING:
		lz = await get_localizer_by_user(current_user)
		await cq.answer(lz.get("submissions.rate.outdated"), show_alert=True)
		await state.clear()
		return
	if mode == "rerate" and submission.status != SubmissionStatus.ACCEPTED:
		lz = await get_localizer_by_user(current_user)
		await cq.answer(lz.get("submissions.rate.outdated"), show_alert=True)
		await state.clear()
		return

	update_payload_kwargs = {"id": submission_id, "status": new_status}
	if value_input is not None:
		update_payload_kwargs["value"] = value_input

	payload = SubmissionUpdate(**update_payload_kwargs)
	updated = await sub_svc.update_submission(payload)

	lz = await get_localizer_by_user(current_user)
	await cq.message.edit_reply_markup(reply_markup=None)
	await cq.message.answer(
		lz.get(
			"submissions.rate.done",
			submission_id=str(updated.id),
			title=updated.title,
			status=_status_label(lz, updated.status),
			value=_format_value(updated.value),
		)
	)

	# reopen list if there are remaining items
	status_raw = data.get("status_filter")
	prefix = data.get("prefix", RATE_PREFIX)
	header_key = data.get("header_key", "submissions.rate.list_title")
	empty_key = data.get("empty_key", "submissions.rate.empty")
	page = data.get("page", 0)

	await state.set_state(SubmissionModerationFSM.waiting_selection)
	try:
		status_filter = SubmissionStatus(status_raw) if status_raw else None
	except ValueError:
		status_filter = None

	has_items, actual_page = await _send_submission_list(
		cq.message,
		lz,
		status_filter=status_filter,
		page=page,
		prefix=prefix,
		header_key=header_key,
		empty_key=empty_key,
	)

	if has_items:
		await state.update_data(
			page=actual_page,
			submission_id=None,
			value_input=None,
		)
	else:
		await state.clear()
	await cq.answer()
