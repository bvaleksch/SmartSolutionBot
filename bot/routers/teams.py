# bot/routers/teams.py
import re
import uuid
from typing import Optional, List, Dict, Any

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from sqlalchemy.exc import IntegrityError

from smart_solution.db.enums import UiMode, UserRole
from smart_solution.db.schemas.user import UserRead
from smart_solution.db.schemas.team import TeamCreate, TeamUpdate, TeamRead
from smart_solution.bot.filters.action_like import ActionLike
from smart_solution.bot.keyboards.user_keyboard_factory import UserKeyboardFactory
from smart_solution.bot.routers.utils import get_localizer_by_user
from smart_solution.bot.services.user import UserService
from smart_solution.bot.services.team import TeamService
from smart_solution.bot.services.competition import CompetitionService

router = Router(name="teams_admin")

COMP_PAGE_SIZE = 6
TEAM_PAGE_SIZE = 8


# --------- helpers ---------
def _is_admin(user: UserRead) -> bool:
    return str(user.role).lower() == UserRole.ADMIN


def _normalize_slug(value: str) -> str:
    return value.strip().lower()


def _valid_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_]{3,}", value))


async def _team_snapshot(team: TeamRead, comp_svc: CompetitionService, lz) -> str:
    track_line = lz.get("teams.snapshot.no_track")
    competition_line = lz.get("teams.snapshot.no_competition")

    if team.track_id:
        track = await comp_svc.get_track_by_id(team.track_id)
        if track:
            track_line = lz.get(
                "teams.snapshot.track",
                track_title=track.title,
                track_slug=track.slug,
            )
            competition = await comp_svc.get_competition_by_id(track.competition_id)
            if competition:
                competition_line = lz.get(
                    "teams.snapshot.competition",
                    competition_title=competition.title,
                )

    error_line = (
        lz.get("teams.snapshot.error", error=team.error)
        if team.error
        else lz.get("teams.snapshot.no_error")
    )

    lines = [
        lz.get("teams.snapshot.title", title=team.title),
        lz.get("teams.snapshot.slug", slug=team.slug),
        track_line,
        competition_line,
        error_line,
    ]

    return "\n".join(lines)


def _kb_competitions_page(comps: List, page: int, pages: int, lz) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for comp in comps:
        label = lz.get(
            "teams.add.comp_item",
            title=comp.title,
            start=comp.start_at.strftime("%Y-%m-%d"),
        )
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"teams.comp.pick:{comp.id}")
        ])

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text=lz.get("teams.nav.prev"), callback_data=f"teams.comp.page:{page-1}")
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(text=lz.get("teams.nav.next"), callback_data=f"teams.comp.page:{page+1}")
        )
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _open_competitions_page(
    target: Message | CallbackQuery,
    svc: CompetitionService,
    page: int,
    lz,
) -> bool:
    comps, total = await svc.list_competitions_page(page=page, page_size=COMP_PAGE_SIZE)
    if total == 0:
        text = lz.get("teams.add.no_competitions")
        if isinstance(target, Message):
            await target.answer(text)
        else:
            await target.message.edit_text(text)
            await target.answer()
        return False

    pages = max(1, (total + COMP_PAGE_SIZE - 1) // COMP_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    kb = _kb_competitions_page(comps, page, pages, lz)
    text = lz.get("teams.add.pick_competition", page=f"{page + 1}", pages=f"{pages}")

    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    return True


def _kb_tracks(tracks, lz) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=lz.get("teams.add.track_item", title=t.title, slug=t.slug),
                callback_data=f"teams.track.pick:{t.id}",
            )
        ]
        for t in tracks
    ]
    rows.append([
        InlineKeyboardButton(text=lz.get("teams.nav.back"), callback_data="teams.track.back")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_teams_page(teams: List[TeamRead], page: int, pages: int, lz) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for team in teams:
        label = lz.get("teams.item", title=team.title, slug=team.slug)
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"teams.pick:{team.id}")
        ])

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=lz.get("teams.nav.prev"), callback_data=f"teams.page:{page-1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text=lz.get("teams.nav.next"), callback_data=f"teams.page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text=lz.get("teams.nav.back"), callback_data="teams.cancel")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _open_teams_page(
    target: Message | CallbackQuery,
    svc: TeamService,
    page: int,
    lz,
) -> bool:
    items, total = await svc.list_teams_page(page=page, page_size=TEAM_PAGE_SIZE)
    if total == 0:
        text = lz.get("teams.edit.empty")
        if isinstance(target, Message):
            await target.answer(text)
        else:
            await target.message.edit_text(text)
            await target.answer()
        return False

    pages = max(1, (total + TEAM_PAGE_SIZE - 1) // TEAM_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    kb = _kb_teams_page(items, page, pages, lz)
    text = lz.get("teams.edit.pick_team", page=f"{page + 1}", pages=f"{pages}")

    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    return True


def _kb_edit_fields(lz) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=lz.get("teams.fields.title"), callback_data="team.field:title")],
        [InlineKeyboardButton(text=lz.get("teams.fields.slug"), callback_data="team.field:slug")],
        [InlineKeyboardButton(text=lz.get("teams.fields.error"), callback_data="team.field:error")],
        [InlineKeyboardButton(text=lz.get("teams.fields.back"), callback_data="team.field:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------- FSM states ---------
class AddTeamFSM(StatesGroup):
    choose_competition = State()
    choose_track = State()
    title = State()
    slug = State()


class EditTeamFSM(StatesGroup):
    waiting_target = State()
    choose_field = State()
    set_value = State()


# --------- mode entry / navigation ---------
@router.message(ActionLike("buttons.team:*:admin"))
async def team_mode(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.TEAM)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("teams.mode.enter"), reply_markup=keyboard)


@router.message(ActionLike("buttons.back:team:admin"))
async def team_mode_back(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return
    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.HOME)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("mode.home"), reply_markup=keyboard)


# --------- add team ---------
@router.message(ActionLike("buttons.add_team:team:admin"))
async def add_team_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.NEW_TEAM)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("teams.add.begin"), reply_markup=keyboard)
    await state.set_state(AddTeamFSM.choose_competition)
    await _open_competitions_page(message, CompetitionService(), page=0, lz=lz)


@router.message(ActionLike("buttons.cancel:new_team:admin"))
async def add_team_cancel(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.TEAM)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("teams.common.cancelled"), reply_markup=keyboard)


@router.callback_query(F.data.startswith("teams.comp.page:"), AddTeamFSM.choose_competition)
async def add_team_comp_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    page = int(cq.data.split(":")[1])
    await _open_competitions_page(cq, CompetitionService(), page=page, lz=lz)


@router.callback_query(F.data.startswith("teams.comp.pick:"), AddTeamFSM.choose_competition)
async def add_team_pick_comp(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    comp_id = uuid.UUID(cq.data.split(":")[1])
    comp_svc = CompetitionService()
    comp = await comp_svc.get_competition_by_id(comp_id)
    if comp is None:
        await cq.answer(lz.get("teams.add.no_competitions"), show_alert=True)
        return

    tracks = await comp_svc.list_tracks(comp_id)
    if not tracks:
        await cq.answer(lz.get("teams.add.no_tracks"), show_alert=True)
        await _open_competitions_page(cq, comp_svc, page=0, lz=lz)
        return

    await state.update_data(selected_competition_id=str(comp_id), selected_competition_title=comp.title)
    kb = _kb_tracks(tracks, lz)
    await state.set_state(AddTeamFSM.choose_track)
    await cq.answer(lz.get("teams.add.competition_selected", competition=comp.title))
    await cq.message.edit_text(lz.get("teams.add.pick_track", competition=comp.title), reply_markup=kb)


@router.callback_query(F.data == "teams.track.back", AddTeamFSM.choose_track)
async def add_team_track_back(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    await state.set_state(AddTeamFSM.choose_competition)
    await _open_competitions_page(cq, CompetitionService(), page=0, lz=lz)


@router.callback_query(F.data.startswith("teams.track.pick:"), AddTeamFSM.choose_track)
async def add_team_pick_track(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    track_id = uuid.UUID(cq.data.split(":")[1])
    comp_svc = CompetitionService()
    track = await comp_svc.get_track_by_id(track_id)
    if track is None:
        await cq.answer(lz.get("teams.add.no_tracks"), show_alert=True)
        return

    data = await state.get_data()
    comp_title = data.get("selected_competition_title", "")
    await state.update_data(selected_track_id=str(track_id), selected_track_title=track.title)
    await state.set_state(AddTeamFSM.title)
    await cq.answer(lz.get("teams.add.track_selected", track=track.title))
    await cq.message.edit_text(
        lz.get("teams.add.track_confirm", competition=comp_title, track=track.title)
    )
    await cq.message.answer(lz.get("teams.add.step_title"))


@router.message(AddTeamFSM.title, F.text)
async def add_team_title(message: Message, current_user: UserRead, state: FSMContext) -> None:
    title = message.text.strip()
    if not title:
        lz = await get_localizer_by_user(current_user)
        await message.answer(lz.get("teams.add.step_title"))
        return

    await state.update_data(new_team_title=title)
    await state.set_state(AddTeamFSM.slug)
    lz = await get_localizer_by_user(current_user)
    await message.answer(lz.get("teams.add.step_slug"))


@router.message(AddTeamFSM.slug, F.text)
async def add_team_slug(message: Message, current_user: UserRead, state: FSMContext) -> None:
    raw = _normalize_slug(message.text)
    lz = await get_localizer_by_user(current_user)
    if not _valid_slug(raw):
        await message.answer(lz.get("teams.add.bad_slug"))
        return

    data = await state.get_data()
    track_id = data.get("selected_track_id")
    if track_id is None:
        await message.answer(lz.get("teams.edit.not_found"))
        await state.clear()
        return

    team_svc = TeamService()
    comp_svc = CompetitionService()
    try:
        created = await team_svc.create_team(
            TeamCreate(
                title=data.get("new_team_title"),
                slug=raw,
                track_id=uuid.UUID(track_id),
            )
        )
    except IntegrityError:
        await message.answer(lz.get("teams.add.slug_exists"))
        return

    await state.clear()
    usr_svc = UserService()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.TEAM)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    snapshot = await _team_snapshot(created, comp_svc, lz)
    await message.answer(
        lz.get("teams.add.created", title=created.title) + "\n\n" + snapshot,
        reply_markup=keyboard,
    )


# --------- edit team ---------
@router.message(ActionLike("buttons.edit_team:team:admin"))
async def edit_team_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.EDIT_TEAM)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await state.set_state(EditTeamFSM.waiting_target)
    ok = await _open_teams_page(message, TeamService(), page=0, lz=lz)
    if ok:
        await message.answer(lz.get("teams.edit.or_send_slug"), reply_markup=keyboard)
    else:
        await message.answer(lz.get("teams.edit.empty"), reply_markup=keyboard)


@router.message(ActionLike("buttons.cancel:edit_team:admin"))
async def edit_team_cancel(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.TEAM)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("teams.common.cancelled"), reply_markup=keyboard)


@router.message(ActionLike("buttons.back:edit_team:admin"))
async def edit_team_back(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return
    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.TEAM)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("teams.mode.enter"), reply_markup=keyboard)


@router.callback_query(F.data.startswith("teams.page:"), EditTeamFSM.waiting_target)
async def edit_team_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    page = int(cq.data.split(":")[1])
    await _open_teams_page(cq, TeamService(), page=page, lz=lz)


@router.callback_query(F.data == "teams.cancel", EditTeamFSM.waiting_target)
async def edit_team_cancel_inline(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.TEAM)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await cq.answer(lz.get("teams.common.cancelled"))
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.message.answer(lz.get("teams.common.cancelled"), reply_markup=keyboard)


@router.callback_query(F.data.startswith("teams.pick:"), EditTeamFSM.waiting_target)
async def edit_team_pick(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    team_id = uuid.UUID(cq.data.split(":")[1])
    team_svc = TeamService()
    comp_svc = CompetitionService()
    team = await team_svc.get_team(team_id)
    if team is None:
        await cq.answer(lz.get("teams.edit.not_found"), show_alert=True)
        return

    await state.update_data(target_team_id=str(team_id))
    await state.set_state(EditTeamFSM.choose_field)
    snapshot = await _team_snapshot(team, comp_svc, lz)
    await cq.answer()
    await cq.message.edit_text(
        lz.get("teams.edit.choose_field") + "\n\n" + snapshot,
        reply_markup=_kb_edit_fields(lz),
    )


@router.message(EditTeamFSM.waiting_target, F.text.regexp(r"^[a-zA-Z0-9_]{3,}$"))
async def edit_team_by_slug(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    slug = _normalize_slug(message.text)
    team_svc = TeamService()
    target: Optional[TeamRead] = None
    page = 0
    while True:
        items, total = await team_svc.list_teams_page(page=page, page_size=TEAM_PAGE_SIZE)
        if not items:
            break
        target = next((t for t in items if _normalize_slug(t.slug) == slug), None)
        if target:
            break
        page += 1
        if page * TEAM_PAGE_SIZE >= total:
            break

    if target is None:
        await message.answer(lz.get("teams.edit.not_found"))
        return

    comp_svc = CompetitionService()
    await state.update_data(target_team_id=str(target.id))
    await state.set_state(EditTeamFSM.choose_field)
    snapshot = await _team_snapshot(target, comp_svc, lz)
    await message.answer(
        lz.get("teams.edit.team_selected") + "\n\n" + snapshot,
        reply_markup=_kb_edit_fields(lz),
    )


@router.callback_query(F.data.startswith("team.field:"), EditTeamFSM.choose_field)
async def edit_team_choose_field(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    field = cq.data.split(":")[1]
    if field == "back":
        await state.set_state(EditTeamFSM.waiting_target)
        await cq.answer()
        await _open_teams_page(cq, TeamService(), page=0, lz=lz)
        return

    await state.update_data(field=field)
    await state.set_state(EditTeamFSM.set_value)

    prompt_key = "teams.edit.send_value"
    if field == "slug":
        prompt_key = "teams.edit.send_slug"
    elif field == "error":
        prompt_key = "teams.edit.send_error"

    await cq.answer()
    await cq.message.edit_text(lz.get(prompt_key), reply_markup=None)


@router.message(EditTeamFSM.set_value, F.text)
async def edit_team_apply(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    field = data.get("field")
    team_id = data.get("target_team_id")
    if field is None or team_id is None:
        await message.answer(lz.get("teams.edit.not_found"))
        await state.clear()
        return

    team_svc = TeamService()
    comp_svc = CompetitionService()
    team = await team_svc.get_team(uuid.UUID(team_id))
    if team is None:
        await message.answer(lz.get("teams.edit.not_found"))
        await state.clear()
        return

    upd_kwargs: Dict[str, Any] = {"id": uuid.UUID(team_id)}
    raw = message.text.strip()

    if field == "title":
        if not raw:
            await message.answer(lz.get("teams.edit.send_value"))
            return
        upd_kwargs["title"] = raw
    elif field == "slug":
        slug = _normalize_slug(raw)
        if not _valid_slug(slug):
            await message.answer(lz.get("teams.add.bad_slug"))
            return
        upd_kwargs["slug"] = slug
    elif field == "error":
        upd_kwargs["error"] = None if raw == "-" else raw
    else:
        await message.answer(lz.get("teams.edit.unsupported"))
        return

    try:
        updated = await team_svc.update_team(TeamUpdate(**upd_kwargs))
    except IntegrityError:
        await message.answer(lz.get("teams.add.slug_exists"))
        return

    await state.set_state(EditTeamFSM.choose_field)
    snapshot = await _team_snapshot(updated, comp_svc, lz)
    field_name = lz.get(f"teams.fields.{field}") if field in {"title", "slug", "error"} else field
    await message.answer(
        lz.get("teams.edit.updated", title=updated.title, field=field_name) + "\n\n" + snapshot,
        reply_markup=_kb_edit_fields(lz),
    )
