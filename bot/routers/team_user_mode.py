# bot/routers/team_user_mode.py
import math
import uuid
from typing import List, Optional

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from smart_solution.db.enums import UiMode, SortDirection
from smart_solution.db.schemas.user import UserRead, UserUpdate
from smart_solution.bot.filters.action_like import ActionLike
from smart_solution.bot.keyboards.user_keyboard_factory import UserKeyboardFactory
from smart_solution.bot.routers.utils import get_localizer_by_user
from smart_solution.bot.services.user import UserService
from smart_solution.bot.services.team import TeamService
from smart_solution.bot.services.competition import CompetitionService
from smart_solution.bot.services.page import PageService

router = Router(name="team_user_mode")

_LEADERBOARD_PAGE_SIZE = 10


async def _ensure_team_selected(user: UserRead, message: Message) -> Optional[uuid.UUID]:
    if (await TeamService().has_selected_team(user)):
        return (await TeamService().get_selected_team_by_user(user)).id
    lz = await get_localizer_by_user(user)
    await message.answer(lz.get("team_user.errors.no_team"))
    return None



@router.message(ActionLike("buttons.leaderboard:home:contestant"))
async def show_leaderboard(message: Message, current_user: UserRead, state: FSMContext) -> None:
    team_id = await _ensure_team_selected(current_user, message)
    if team_id is None:
        await _clear_leaderboard_state(state)
        return

    team_svc = TeamService()
    team = await team_svc.get_team(team_id)
    lz = await get_localizer_by_user(current_user)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)

    if team is None or team.track_id is None:
        await _clear_leaderboard_state(state)
        await message.answer(lz.get("team_user.leaderboard.no_track"), reply_markup=keyboard)
        return

    comp_svc = CompetitionService()
    track, competition, rows = await comp_svc.get_track_leaderboard(team.track_id)
    if track is None or competition is None:
        await _clear_leaderboard_state(state)
        await message.answer(lz.get("team_user.leaderboard.not_found"), reply_markup=keyboard)
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

    if not rows:
        empty_text = lz.get("team_user.leaderboard.empty")
        await _clear_leaderboard_state(state)
        await message.answer(f"{header}\n\n{empty_text}", reply_markup=keyboard)
        return

    if all(row.best_value is None for row in rows):
        no_values_text = lz.get("team_user.leaderboard.no_values")
        await _clear_leaderboard_state(state)
        await message.answer(f"{header}\n\n{no_values_text}", reply_markup=keyboard)
        return

    total_pages = math.ceil(len(rows) / _LEADERBOARD_PAGE_SIZE)
    serialized_rows = [
        {
            "team_id": str(row.team_id),
            "team_title": row.team_title,
            "best_value": row.best_value,
            "submission_count": row.submission_count,
        }
        for row in rows
    ]

    if total_pages <= 1:
        body = _render_leaderboard_page(serialized_rows, 0, team.id, lz)
        text = f"{header}\n\n{body}"
        await _clear_leaderboard_state(state)
        await message.answer(text, reply_markup=keyboard)
        return

    page_text = _render_leaderboard_page(serialized_rows, 0, team.id, lz)
    page_status = lz.get(
        "team_user.leaderboard.page_status",
        current="1",
        total=str(total_pages),
    )
    text = f"{header}\n{page_status}\n\n{page_text}"
    reply_markup = _build_leaderboard_keyboard(0, total_pages, lz)
    sent = await message.answer(text, reply_markup=reply_markup)
    await state.update_data(
        leaderboard_rows=serialized_rows,
        leaderboard_header=header,
        leaderboard_team_id=str(team.id),
        leaderboard_page=0,
        leaderboard_total_pages=total_pages,
        leaderboard_message_id=sent.message_id,
    )

@router.message(ActionLike("buttons.team:home:contestant"))
async def enter_team_mode(message: Message, current_user: UserRead) -> None:
    team_id = await _ensure_team_selected(current_user, message)
    if team_id is None:
        return

    user_svc = UserService()
    current_user = await user_svc.change_ui_mode(current_user, UiMode.TEAM)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)

    team = await TeamService().get_team(team_id)
    if team is None:
        lz = await get_localizer_by_user(current_user)
        await message.answer(lz.get("team_user.errors.no_team"), reply_markup=keyboard)
        return

    comp_svc = CompetitionService()
    track = await comp_svc.get_track_by_id(team.track_id) if team.track_id else None
    lz = await get_localizer_by_user(current_user)

    text = lz.get(
        "team_user.team.info",
        team=team.title,
        track=track.title if track else lz.get("team_user.team.no_track"),
    )
    await message.answer(text, reply_markup=keyboard)



def _format_value(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"

async def _load_page(team_id: uuid.UUID, page_slug: str, user: UserRead) -> Optional[str]:
    team_svc = TeamService()
    team = await team_svc.get_team(team_id)
    if team is None or team.track_id is None:
        return None

    comp_svc = CompetitionService()
    track = await comp_svc.get_track_by_id(team.track_id)
    if track is None:
        return None

    competition = await comp_svc.get_competition_by_id(track.competition_id)
    if competition is None:
        return None

    page_service = PageService()
    preferred_language = user.preferred_language_id
    page_slug_with_track = f"{page_slug}_{track.slug}"

    page = None
    if preferred_language:
        page = await page_service.get_page(competition.id, page_slug_with_track, preferred_language)
    if page is None:
        page = await page_service.get_page(competition.id, page_slug_with_track)

    if page is None:
        return None

    file_path = page_service.get_content_path(page.file_basename)
    if not file_path.exists():
        return None
    return file_path.read_text(encoding="utf-8")


@router.message(ActionLike("buttons.about:team:contestant"))
async def show_about(message: Message, current_user: UserRead) -> None:
    team_id = await _ensure_team_selected(current_user, message)
    if team_id is None:
        return

    content = await _load_page(team_id, "about", current_user)
    lz = await get_localizer_by_user(current_user)
    if content is None:
        await message.answer(lz.get("team_user.pages.missing"))
        return

    await message.answer(content)

@router.callback_query(F.data.startswith("team_user.leaderboard.page:"))
async def paginate_leaderboard(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()

    rows: Optional[List[dict]] = data.get("leaderboard_rows")
    header: Optional[str] = data.get("leaderboard_header")
    total_pages: int = data.get("leaderboard_total_pages") or 0
    message_id: Optional[int] = data.get("leaderboard_message_id")
    stored_page: int = data.get("leaderboard_page") or 0
    team_id = data.get("leaderboard_team_id")

    if cq.message is None or message_id != cq.message.message_id or not rows or not header or not team_id:
        await cq.answer(lz.get("team_user.leaderboard.expired"), show_alert=True)
        await _clear_leaderboard_state(state)
        return

    try:
        requested_page = int(cq.data.split(":")[1])
    except (IndexError, ValueError):
        await cq.answer()
        return

    if total_pages <= 1:
        await cq.answer()
        return

    requested_page = max(0, min(requested_page, total_pages - 1))
    if requested_page == stored_page:
        await cq.answer()
        return

    try:
        team_uuid = uuid.UUID(team_id)
    except ValueError:
        await cq.answer(lz.get("team_user.leaderboard.expired"), show_alert=True)
        await _clear_leaderboard_state(state)
        return

    page_status = lz.get(
        "team_user.leaderboard.page_status",
        current=str(requested_page + 1),
        total=str(total_pages),
    )
    body = _render_leaderboard_page(rows, requested_page, team_uuid, lz)
    text = f"{header}\n{page_status}\n\n{body}"
    reply_markup = _build_leaderboard_keyboard(requested_page, total_pages, lz)

    try:
        await cq.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        # Ignore edit errors (e.g. no changes); do not break pagination flow.
        await cq.answer()
        return

    await state.update_data(leaderboard_page=requested_page)
    await cq.answer()

@router.callback_query(F.data == "team_user.leaderboard.ignore")
async def leaderboard_ignore(cq: CallbackQuery) -> None:
    await cq.answer()

@router.message(ActionLike("buttons.rule:team:contestant"))
async def show_rules(message: Message, current_user: UserRead) -> None:
    team_id = await _ensure_team_selected(current_user, message)
    if team_id is None:
        return

    content = await _load_page(team_id, "rule", current_user)
    lz = await get_localizer_by_user(current_user)
    if content is None:
        await message.answer(lz.get("team_user.pages.missing"))
        return

    await message.answer(content)


def _build_team_choice_keyboard(options: List[dict], page: int, pages: int, lz) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for option in options:
        rows.append(
            [
                InlineKeyboardButton(
                    text=lz.get(
                        "team_user.switch.item",
                        team=option["title"],
                        track=option["track"],
                    ),
                    callback_data=f"team_user.team.pick:{option['id']}",
                )
            ]
        )

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text=lz.get("team_user.nav.prev"), callback_data=f"team_user.team.page:{page-1}"
            )
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                text=lz.get("team_user.nav.next"), callback_data=f"team_user.team.page:{page+1}"
            )
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=lz.get("team_user.nav.back"), callback_data="team_user.team.cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _collect_user_teams(user: UserRead, team_svc: TeamService, comp_svc: CompetitionService):
    teams = await team_svc.get_user_teams(user)
    result: List[dict] = []
    for team in teams:
        if team.track_id is None:
            track_title = "—"
        else:
            track = await comp_svc.get_track_by_id(team.track_id)
            track_title = track.title if track else "—"
        result.append(
            {
                "id": str(team.id),
                "title": team.title,
                "track": track_title,
            }
        )
    result.sort(key=lambda x: x["title"].lower())
    return result


@router.message(ActionLike("buttons.change_team:team:contestant"))
async def change_team_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
    team_svc = TeamService()
    comp_svc = CompetitionService()
    lz = await get_localizer_by_user(current_user)

    options = await _collect_user_teams(current_user, team_svc, comp_svc)
    if not options:
        await message.answer(lz.get("team_user.switch.none"))
        return

    await state.update_data(change_team_options=options, change_team_page=0)
    kb = _build_team_choice_keyboard(options[:5], 0, max(1, (len(options) + 4) // 5), lz)
    await message.answer(lz.get("team_user.switch.pick", count=str(len(options))), reply_markup=kb)


@router.callback_query(F.data.startswith("team_user.team.page:"))
async def change_team_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    options: List[dict] = data.get("change_team_options", [])
    if not options:
        await cq.answer(lz.get("team_user.switch.none"), show_alert=True)
        return

    page = int(cq.data.split(":")[1])
    total_pages = max(1, (len(options) + 4) // 5)
    page = max(0, min(page, total_pages - 1))
    await state.update_data(change_team_page=page)
    page_items = options[page * 5 : (page + 1) * 5]
    kb = _build_team_choice_keyboard(page_items, page, total_pages, lz)

    await cq.message.edit_reply_markup(reply_markup=kb)
    await cq.answer()


@router.callback_query(F.data == "team_user.team.cancel")
async def change_team_cancel(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    await state.update_data(change_team_options=None)
    await cq.answer()
    await cq.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("team_user.team.pick:"))
async def change_team_pick(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    team_id = uuid.UUID(cq.data.split(":")[1])

    user_svc = UserService()
    updated_user = await user_svc.update_user(UserUpdate(id=current_user.id, active_team_id=team_id))

    await state.update_data(change_team_options=None)
    await cq.answer(lz.get("team_user.switch.done"))
    await cq.message.edit_reply_markup(reply_markup=None)

    keyboard = await UserKeyboardFactory().build_for_user(updated_user)
    team = await TeamService().get_team(team_id)
    if team is None:
        await cq.message.answer(lz.get("team_user.errors.no_team"), reply_markup=keyboard)
        return

    comp_svc = CompetitionService()
    track = await comp_svc.get_track_by_id(team.track_id) if team.track_id else None
    text = lz.get(
        "team_user.team.info",
        team=team.title,
        track=track.title if track else lz.get("team_user.team.no_track"),
    )
    await cq.message.answer(text, reply_markup=keyboard)


@router.message(ActionLike("buttons.back:team:contestant"))
async def team_back_home(message: Message, current_user: UserRead) -> None:
    user_svc = UserService()
    current_user = await user_svc.change_ui_mode(current_user, UiMode.HOME)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    lz = await get_localizer_by_user(current_user)
    await message.answer(lz.get("mode.home"), reply_markup=keyboard)


async def _clear_leaderboard_state(state: FSMContext) -> None:
    await state.update_data(
        leaderboard_rows=None,
        leaderboard_header=None,
        leaderboard_team_id=None,
        leaderboard_page=None,
        leaderboard_total_pages=None,
        leaderboard_message_id=None,
    )


def _render_leaderboard_page(
    rows: List[dict],
    page: int,
    team_id: uuid.UUID | str,
    lz,
) -> str:
    page = max(page, 0)
    start = page * _LEADERBOARD_PAGE_SIZE
    slice_rows = rows[start : start + _LEADERBOARD_PAGE_SIZE]
    if not slice_rows:
        return lz.get("team_user.leaderboard.empty")

    team_id_str = str(team_id)
    value_none = lz.get("team_user.leaderboard.value_none")
    lines_out: List[str] = []
    for idx, row in enumerate(slice_rows, start=start + 1):
        value = row.get("best_value")
        value_text = _format_value(value) if value is not None else value_none
        key = "team_user.leaderboard.row_current" if row.get("team_id") == team_id_str else "team_user.leaderboard.row"
        lines_out.append(
            lz.get(
                key,
                index=str(idx),
                team=row.get("team_title", "—"),
                value=value_text,
                submissions=str(row.get("submission_count", 0)),
            )
        )
    return "\n".join(lines_out)


def _build_leaderboard_keyboard(page: int, total_pages: int, lz) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    nav_row: List[InlineKeyboardButton] = []

    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text=lz.get("team_user.nav.prev"),
                callback_data=f"team_user.leaderboard.page:{page-1}",
            )
        )

    nav_row.append(
        InlineKeyboardButton(
            text=lz.get(
                "team_user.leaderboard.page_status",
                current=str(page + 1),
                total=str(total_pages),
            ),
            callback_data="team_user.leaderboard.ignore",
        )
    )

    if page + 1 < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text=lz.get("team_user.nav.next"),
                callback_data=f"team_user.leaderboard.page:{page+1}",
            )
        )

    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)
