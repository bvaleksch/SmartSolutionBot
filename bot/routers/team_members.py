# bot/routers/team_members.py
import math
import uuid
from typing import Any, Dict, List, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from smart_solution.db.enums import ContestantRole, UiMode, UserRole
from smart_solution.db.schemas.user import UserRead
from smart_solution.db.schemas.team_user import TeamUserCreate
from smart_solution.db.schemas.user import UserUpdate
from smart_solution.bot.filters.action_like import ActionLike
from smart_solution.bot.keyboards.user_keyboard_factory import UserKeyboardFactory
from smart_solution.bot.routers.utils import get_localizer_by_user
from smart_solution.bot.services.team import TeamService
from smart_solution.bot.services.user import UserService
from smart_solution.bot.services.competition import CompetitionService

router = Router(name="team_members_admin")

TEAM_PAGE_SIZE = 6
USER_PAGE_SIZE = 8


def _is_admin(user: UserRead) -> bool:
    return str(user.role).lower() == UserRole.ADMIN


def _format_username_value(username: Optional[str]) -> str:
    return f"@{username}" if username else "—"


def _format_full_name_value(first_name: Optional[str], last_name: Optional[str], username: Optional[str]) -> str:
    parts = [first_name or "", last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name if name else _format_username_value(username)


def _build_team_button_text(entry: Dict[str, Any], lz) -> str:
    return lz.get(
        "team_members.teams.item",
        title=entry["title"],
        slug=entry["slug"],
        members=str(entry["members"]),
        capacity=str(entry["capacity"]),
    )


def _build_user_button_text(user_info: Dict[str, Any], lz) -> str:
    return lz.get(
        "team_members.users.item",
        name=_format_full_name_value(user_info.get("first_name"), user_info.get("last_name"), user_info.get("username")),
        username=_format_username_value(user_info.get("username")),
    )


class IncludeMemberFSM(StatesGroup):
    choose_team = State()
    choose_user = State()


async def _load_team_options(state: FSMContext, team_svc: TeamService) -> List[Dict[str, Any]]:
    data = await state.get_data()
    options = data.get("team_options")
    if options is not None:
        return options

    team_infos = await team_svc.available_team_infos()
    serialized: List[Dict[str, Any]] = []
    for info in team_infos:
        team: TeamRead = info["team"]
        serialized.append(
            {
                "id": str(team.id),
                "title": team.title,
                "slug": team.slug,
                "track_title": info["track"].title,
                "competition_title": info["competition"].title,
                "members": info["member_count"],
                "capacity": info["capacity"],
            }
        )

    await state.update_data(team_options=serialized, team_page=0)
    return serialized


def _slice_page(items: List[Any], page: int, page_size: int) -> List[Any]:
    start = page * page_size
    end = start + page_size
    return items[start:end]


async def _show_team_page(
    target: Message | CallbackQuery,
    state: FSMContext,
    lz,
    team_svc: TeamService,
    page: int,
) -> bool:
    options = await _load_team_options(state, team_svc)
    if not options:
        text = lz.get("team_members.teams.none")
        if isinstance(target, Message):
            await target.answer(text)
        else:
            await target.message.edit_text(text)
            await target.answer()
        return False

    total_pages = math.ceil(len(options) / TEAM_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    await state.update_data(team_page=page)
    page_items = _slice_page(options, page, TEAM_PAGE_SIZE)

    rows: List[List[InlineKeyboardButton]] = []
    for entry in page_items:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_build_team_button_text(entry, lz),
                    callback_data=f"teamadd.team.pick:{entry['id']}",
                )
            ]
        )

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=lz.get("team_members.nav.prev"), callback_data=f"teamadd.team.page:{page-1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text=lz.get("team_members.nav.next"), callback_data=f"teamadd.team.page:{page+1}"))
    if nav:
        rows.append(nav)

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = lz.get("team_members.teams.pick", page=f"{page + 1}", pages=f"{total_pages}")

    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    return True


async def _load_user_options(team_id: uuid.UUID, team_svc: TeamService) -> List[Dict[str, Any]]:
    user_svc = UserService()
    users: List[Dict[str, Any]] = []
    page = 0
    page_size = 50
    while True:
        items, total = await user_svc.list_users_page(page=page, page_size=page_size)
        if not items:
            break
        for user in items:
            if not await team_svc.is_user_in_team(team_id, user.id):
                users.append(
                    {
                        "id": str(user.id),
                        "first_name": user.first_name,
                        "last_name": user.last_name,
                        "username": user.tg_username,
                    }
                )
        page += 1
        if page * page_size >= total:
            break
    users.sort(
        key=lambda info: (
            _format_full_name_value(info.get("first_name"), info.get("last_name"), info.get("username")).lower(),
            _format_username_value(info.get("username")),
        )
    )
    return users


async def _show_user_page(
    target: Message | CallbackQuery,
    state: FSMContext,
    lz,
    users: List[Dict[str, Any]],
    page: int,
    team_title: str,
) -> bool:
    if not users:
        if isinstance(target, Message):
            await target.answer(lz.get("team_members.users.none"))
        else:
            await target.message.edit_text(lz.get("team_members.users.none"))
            await target.answer()
        return False

    total_pages = math.ceil(len(users) / USER_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    await state.update_data(user_page=page)
    page_users = _slice_page(users, page, USER_PAGE_SIZE)

    rows: List[List[InlineKeyboardButton]] = []
    for user in page_users:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_build_user_button_text(user, lz),
                    callback_data=f"teamadd.user.pick:{user['id']}",
                )
            ]
        )

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=lz.get("team_members.nav.prev"), callback_data=f"teamadd.user.page:{page-1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text=lz.get("team_members.nav.next"), callback_data=f"teamadd.user.page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text=lz.get("team_members.nav.back"), callback_data="teamadd.user.back")
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = lz.get("team_members.users.pick", team=team_title, page=f"{page + 1}", pages=f"{total_pages}")

    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    return True


@router.message(ActionLike("buttons.include_user_team:user:admin"))
async def include_user_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    lz = await get_localizer_by_user(current_user)
    team_svc = TeamService()
    await state.clear()
    await state.set_state(IncludeMemberFSM.choose_team)
    await message.answer(lz.get("team_members.teams.start"))
    success = await _show_team_page(message, state, lz, team_svc, page=0)
    if not success:
        await state.clear()


@router.message(ActionLike("buttons.cancel:user:admin"), IncludeMemberFSM.choose_user)
@router.message(ActionLike("buttons.cancel:user:admin"), IncludeMemberFSM.choose_team)
async def include_user_cancel(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    lz = await get_localizer_by_user(current_user)
    await state.clear()
    await message.answer(lz.get("team_members.common.cancelled"))


@router.callback_query(F.data.startswith("teamadd.team.page:"), IncludeMemberFSM.choose_team)
async def include_team_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    team_svc = TeamService()
    page = int(cq.data.split(":")[1])
    await _show_team_page(cq, state, lz, team_svc, page)


@router.callback_query(F.data.startswith("teamadd.team.pick:"), IncludeMemberFSM.choose_team)
async def include_team_pick(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    team_id = uuid.UUID(cq.data.split(":")[1])
    team_svc = TeamService()

    team = await team_svc.get_team(team_id)
    if team is None:
        await cq.answer(lz.get("team_members.teams.none"), show_alert=True)
        await state.update_data(team_options=None)
        await _show_team_page(cq, state, lz, team_svc, page=0)
        return

    if await team_svc.is_competition_finished(team):
        await cq.answer(lz.get("team_members.common.competition_finished", team=team.title), show_alert=True)
        await state.update_data(team_options=None)
        await _show_team_page(cq, state, lz, team_svc, page=0)
        return

    if await team_svc.is_team_full(team):
        await cq.answer(lz.get("team_members.common.team_full", team=team.title), show_alert=True)
        await state.update_data(team_options=None)
        await _show_team_page(cq, state, lz, team_svc, page=0)
        return

    users = await _load_user_options(team_id, team_svc)
    if not users:
        await cq.answer(lz.get("team_members.users.none"), show_alert=True)
        await state.update_data(team_options=None)
        await _show_team_page(cq, state, lz, team_svc, page=0)
        return

    member_count = await team_svc.team_member_count(team_id)
    capacity = len(users) + member_count  # capacity will adjust below from team info
    track = None
    if team.track_id:
        track = await CompetitionService().get_track_by_id(team.track_id)
        if track:
            capacity = int(track.max_contestants)

    await state.update_data(
        selected_team_id=str(team_id),
        selected_team_title=team.title,
        team_member_count=member_count,
        team_capacity=capacity,
        available_users=users,
        user_page=0,
        team_options=None,  # force refresh next time
    )

    await state.set_state(IncludeMemberFSM.choose_user)
    await cq.answer(
        lz.get("team_members.teams.selected", team=team.title),
    )
    await _show_user_page(cq, state, lz, users, page=0, team_title=team.title)


@router.callback_query(F.data == "teamadd.user.back", IncludeMemberFSM.choose_user)
async def include_user_back(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    team_svc = TeamService()
    await state.update_data(selected_team_id=None, available_users=None)
    await state.set_state(IncludeMemberFSM.choose_team)
    await cq.answer()
    await _show_team_page(cq, state, lz, team_svc, page=0)


@router.callback_query(F.data.startswith("teamadd.user.page:"), IncludeMemberFSM.choose_user)
async def include_user_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    users = data.get("available_users") or []
    team_title = data.get("selected_team_title", "")
    page = int(cq.data.split(":")[1])
    await _show_user_page(cq, state, lz, users, page, team_title)


@router.callback_query(F.data.startswith("teamadd.user.pick:"), IncludeMemberFSM.choose_user)
async def include_user_pick(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    team_id_raw = data.get("selected_team_id")
    if team_id_raw is None:
        await cq.answer(lz.get("team_members.teams.none"), show_alert=True)
        await state.set_state(IncludeMemberFSM.choose_team)
        await include_user_back(cq, current_user, state)
        return

    team_id = uuid.UUID(team_id_raw)
    team_svc = TeamService()
    team = await team_svc.get_team(team_id)
    if team is None:
        await cq.answer(lz.get("team_members.teams.none"), show_alert=True)
        await include_user_back(cq, current_user, state)
        return

    if await team_svc.is_competition_finished(team):
        await cq.answer(lz.get("team_members.common.competition_finished", team=team.title), show_alert=True)
        await include_user_back(cq, current_user, state)
        return

    member_count = await team_svc.team_member_count(team_id)
    capacity = data.get("team_capacity", 0)
    if member_count >= capacity:
        await cq.answer(lz.get("team_members.common.team_full", team=team.title), show_alert=True)
        await include_user_back(cq, current_user, state)
        return

    user_id = uuid.UUID(cq.data.split(":")[1])
    user_svc = UserService()
    user = await user_svc.get_user(uid=user_id)
    if user is None:
        await cq.answer(lz.get("team_members.users.none"), show_alert=True)
        return

    if await team_svc.is_user_in_team(team_id, user_id):
        await cq.answer(lz.get("team_members.common.already_in_team", name=_format_full_name_value(user)), show_alert=True)
        return

    role = ContestantRole.CAPTAIN if member_count == 0 else ContestantRole.MEMBER
    await team_svc.upsert_team_user(
        TeamUserCreate(role=role, user_id=user_id, team_id=team_id)
    )

    member_count = await team_svc.team_member_count(team_id)
    await state.update_data(team_member_count=member_count, team_options=None)

    if not (await team_svc.has_selected_team(user)):
        user = await user_svc.update_user(UserUpdate(id=user.id, active_team_id=team_id))

    users_list: List[Dict[str, Any]] = data.get("available_users") or []
    cleaned_users: List[Dict[str, Any]] = [entry for entry in users_list if entry.get("id") != str(user_id)]

    await state.update_data(available_users=cleaned_users, user_page=0)

    name_display = _format_full_name_value(user.first_name, user.last_name, user.tg_username)
    if role == ContestantRole.CAPTAIN:
        await cq.answer()
        await cq.message.answer(lz.get("team_members.users.added_captain", name=name_display))
    else:
        await cq.answer()
        await cq.message.answer(lz.get("team_members.users.added_member", name=name_display))

    await cq.message.answer(
        lz.get(
            "team_members.status.count",
            team=team.title,
            members=str(member_count),
            capacity=str(data.get("team_capacity", 0)),
        )
    )

    if member_count >= data.get("team_capacity", 0):
        await cq.message.answer(lz.get("team_members.teams.full_after", team=team.title))
        await include_user_back(cq, current_user, state)
        return

    if not cleaned_users:
        await cq.message.answer(lz.get("team_members.users.none"))
        await include_user_back(cq, current_user, state)
        return

    await _show_user_page(cq, state, lz, cleaned_users, page=0, team_title=team.title)
