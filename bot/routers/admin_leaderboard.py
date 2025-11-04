# bot/routers/admin_leaderboard.py
import math
import uuid
from typing import Optional, List

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from smart_solution.bot.filters.action_like import ActionLike
from smart_solution.bot.routers.utils import get_localizer_by_user
from smart_solution.bot.services.competition import CompetitionService
from smart_solution.bot.services.team import TeamService
from smart_solution.bot.services.user import UserService
from smart_solution.db.enums import UiMode, UserRole, SortDirection
from smart_solution.db.schemas.user import UserRead, UserUpdate


router = Router(name="admin_leaderboard")


class AdminLeaderboardFSM(StatesGroup):
    waiting_competition = State()
    waiting_track = State()


def _is_admin(user: UserRead) -> bool:
    return str(user.role).lower() == UserRole.ADMIN.value


def _format_value(value: Optional[float]) -> str:
    if value is None:
        return "—"
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _build_paged_keyboard(items: List[tuple[str, str]], page: int, pages: int, prefix: str, lz) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for item_id, label in items:
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}.pick:{item_id}")])

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=lz.get("competitions.nav.prev"), callback_data=f"{prefix}.page:{page-1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text=lz.get("competitions.nav.next"), callback_data=f"{prefix}.page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=lz.get("team_user.nav.back"), callback_data=f"{prefix}.cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_competition_list(target: Message | CallbackQuery, state: FSMContext, page: int, lz) -> None:
    svc = CompetitionService()
    competitions, total = await svc.list_competitions_page(page=page, page_size=6)
    if not competitions:
        text = lz.get("competitions.admin_lb.empty")
        if isinstance(target, Message):
            await target.answer(text)
        else:
            await target.answer(text, show_alert=True)
        return

    pages = max(1, math.ceil(total / 6))
    page = max(0, min(page, pages - 1))
    items = []
    for comp in competitions:
        label = lz.get("competitions.item", title=comp.title, start=_format_datetime_moscow(comp.start_at))
        items.append((str(comp.id), label))
    kb = _build_paged_keyboard(items, page, pages, "admin_lb.comp", lz)
    text = lz.get("competitions.admin_lb.pick_comp", page=str(page + 1), pages=str(pages))

    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    await state.update_data(admin_lb_comp_page=page)


async def _send_track_list(target: Message | CallbackQuery, state: FSMContext, comp_id: uuid.UUID, page: int, lz) -> None:
    svc = CompetitionService()
    tracks = await svc.list_tracks(comp_id)
    if not tracks:
        await state.update_data(admin_lb_tracks=None)
        text = lz.get("competitions.admin_lb.no_tracks")
        if isinstance(target, Message):
            await target.answer(text)
        else:
            await target.answer(text, show_alert=True)
        return

    tracks.sort(key=lambda t: t.title.lower())
    await state.update_data(admin_lb_tracks=[str(t.id) for t in tracks])
    page_size = 6
    pages = max(1, math.ceil(len(tracks) / page_size))
    page = max(0, min(page, pages - 1))
    slice_tracks = tracks[page * page_size : (page + 1) * page_size]
    items = []
    svc_comp = await svc.get_competition_by_id(comp_id)
    for tr in slice_tracks:
        direction = lz.get(
            "team_user.leaderboard.direction_desc"
            if tr.sort_by == SortDirection.DESC
            else "team_user.leaderboard.direction_asc"
        )
        label = lz.get("competitions.admin_lb.track_item", title=tr.title, direction=direction)
        items.append((str(tr.id), label))
    kb = _build_paged_keyboard(items, page, pages, "admin_lb.track", lz)
    header = svc_comp.title if svc_comp else "—"
    text = lz.get("competitions.admin_lb.pick_track", competition=header, page=str(page + 1), pages=str(pages))

    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    await state.update_data(admin_lb_track_page=page)


def _format_datetime_moscow(dt):
    from zoneinfo import ZoneInfo
    from datetime import datetime

    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M")


def _render_rows(rows, lz) -> str:
    if not rows:
        return lz.get("team_user.leaderboard.empty")
    value_none = lz.get("team_user.leaderboard.value_none")
    lines = []
    for idx, row in enumerate(rows, start=1):
        value_text = _format_value(row.best_value) if row.best_value is not None else value_none
        lines.append(
            lz.get(
                "team_user.leaderboard.row",
                index=str(idx),
                team=row.team_title,
                value=value_text,
                submissions=str(row.submission_count),
            )
        )
    return "\n".join(lines)


@router.message(ActionLike("buttons.leaderboard:home:admin"))
async def admin_lb_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return
    lz = await get_localizer_by_user(current_user)
    await state.set_state(AdminLeaderboardFSM.waiting_competition)
    await _send_competition_list(message, state, 0, lz)


@router.callback_query(AdminLeaderboardFSM.waiting_competition, F.data.startswith("admin_lb.comp.page:"))
async def admin_lb_comp_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        await cq.answer()
        return
    lz = await get_localizer_by_user(current_user)
    page = int(cq.data.split(":")[1])
    await _send_competition_list(cq, state, page, lz)


@router.callback_query(AdminLeaderboardFSM.waiting_competition, F.data == "admin_lb.comp.cancel")
async def admin_lb_comp_cancel(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    await state.clear()
    await cq.answer()
    await cq.message.edit_reply_markup(None)


@router.callback_query(AdminLeaderboardFSM.waiting_competition, F.data.startswith("admin_lb.comp.pick:"))
async def admin_lb_comp_pick(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        await cq.answer()
        return
    comp_id = uuid.UUID(cq.data.split(":")[1])
    await state.update_data(admin_lb_competition=str(comp_id))
    await state.set_state(AdminLeaderboardFSM.waiting_track)
    lz = await get_localizer_by_user(current_user)
    await _send_track_list(cq, state, comp_id, 0, lz)


@router.callback_query(AdminLeaderboardFSM.waiting_track, F.data.startswith("admin_lb.track.page:"))
async def admin_lb_track_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    data = await state.get_data()
    comp_id_raw = data.get("admin_lb_competition")
    if not comp_id_raw:
        await cq.answer()
        return
    comp_id = uuid.UUID(comp_id_raw)
    lz = await get_localizer_by_user(current_user)
    page = int(cq.data.split(":")[1])
    await _send_track_list(cq, state, comp_id, page, lz)


@router.callback_query(AdminLeaderboardFSM.waiting_track, F.data == "admin_lb.track.cancel")
async def admin_lb_track_cancel(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    await state.clear()
    await cq.answer()
    await cq.message.edit_text(lz.get("competitions.admin_lb.cancelled"), reply_markup=None)


@router.callback_query(AdminLeaderboardFSM.waiting_track, F.data.startswith("admin_lb.track.pick:"))
async def admin_lb_track_pick(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    data = await state.get_data()
    comp_id_raw = data.get("admin_lb_competition")
    if not comp_id_raw:
        await cq.answer()
        return
    comp_id = uuid.UUID(comp_id_raw)
    track_id = uuid.UUID(cq.data.split(":")[1])
    lz = await get_localizer_by_user(current_user)
    svc = CompetitionService()
    track, competition, rows = await svc.get_track_leaderboard(track_id)
    if track is None or competition is None:
        await cq.answer(lz.get("team_user.leaderboard.not_found"), show_alert=True)
        return

    direction_key = (
        "team_user.leaderboard.direction_desc"
        if track.sort_by == SortDirection.DESC
        else "team_user.leaderboard.direction_asc"
    )
    header = lz.get(
        "team_user.leaderboard.header",
        competition=competition.title,
        track=track.title,
        direction=lz.get(direction_key),
    )

    body = _render_rows(rows, lz)
    await state.clear()
    await cq.answer()
    await cq.message.edit_text(f"{header}\n\n{body}", reply_markup=None)
