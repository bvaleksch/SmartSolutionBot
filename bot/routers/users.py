# bot/routers/users.py
import re
import uuid
from typing import Optional, List
from aiogram import Router, F
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from smart_solution.i18n import Localizer
from smart_solution.db.schemas.user import UserRead, UserCreate, UserUpdate
from smart_solution.db.enums import UiMode, UserRole
from smart_solution.bot.filters.action_like import ActionLike
from smart_solution.bot.services.user import UserService
from smart_solution.bot.keyboards.user_keyboard_factory import UserKeyboardFactory
from smart_solution.bot.routers.utils import get_localizer_by_user
from smart_solution.bot.services.audit_log import instrument_router_module

router = Router(name="users_admin")
PAGE_SIZE = 8  # deterministic paging by UUID

ROLE_VALUES: dict[str, UserRole] = {
    "admin": UserRole.ADMIN,
    "contestant": UserRole.CONTESTANT,
    "unregistered": UserRole.UNREGISTERED,
}

# ---------- helpers ----------
def _is_admin(u: UserRead) -> bool:
    return str(u.role).lower() == "admin"

def _norm_username(username: str) -> str:
    username = username.strip()
    return username[1:] if username.startswith("@") else username

def _kb_users_page(users: List[UserRead], page: int, pages: int, lz: Localizer) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for u in users:
        tag = f"@{u.tg_username}" if u.tg_username else "—"
        fn = u.first_name or ""
        ln = u.last_name or ""
        rows.append([
            InlineKeyboardButton(
                text=lz.get("users.item", username=tag, first_name=fn, last_name=ln),
                callback_data=f"users.pick:{u.id}"
            )
        ])
    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=lz.get("users.nav.prev"), callback_data=f"users.page:{page-1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text=lz.get("users.nav.next"), callback_data=f"users.page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=lz.get("buttons.cancel"), callback_data="users.cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _kb_role_options(lz: Localizer) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=lz.get("roles.admin"), callback_data="edit.role:set:admin")],
        [InlineKeyboardButton(text=lz.get("roles.contestant"), callback_data="edit.role:set:contestant")],
        [InlineKeyboardButton(text=lz.get("roles.unregistered"), callback_data="edit.role:set:unregistered")],
        [InlineKeyboardButton(text=lz.get("users.nav.back"), callback_data="edit.role:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _format_user_snapshot(user: UserRead, lz: Localizer) -> str:
    """
    Human-friendly snapshot of the selected user for the edit flow.
    """
    def _fmt(value: Optional[str]) -> str:
        return value if value not in {None, ""} else "—"

    username = f"@{user.tg_username}" if user.tg_username else "—"
    role = lz.get(f"roles.{str(user.role).lower()}")
    lines = [
        f"• {lz.get('users.fields.username')}: {username}",
        f"• {lz.get('users.fields.first_name')}: {_fmt(user.first_name)}",
        f"• {lz.get('users.fields.last_name')}: {_fmt(user.last_name)}",
        f"• {lz.get('users.fields.middle_name')}: {_fmt(user.middle_name)}",
        f"• {lz.get('users.fields.email')}: {_fmt(str(user.email) if user.email else None)}",
        f"• {lz.get('users.fields.phone')}: {_fmt(user.phone_number)}",
        f"• {lz.get('users.fields.role')}: {role}",
    ]
    details = "\n".join(lines)
    return lz.get("users.edit.current_info", details=details)

async def _open_page(target: Message | CallbackQuery, svc: UserService, page: int, lz: Localizer) -> None:
    items, total = await svc.list_users_page(page=page, page_size=PAGE_SIZE)  # strictly by UUID ASC
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    kb = _kb_users_page(items, page, pages, lz)
    text = lz.get("users.edit.pick_user", page=f"{page+1}", pages=f"{pages}")

    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()

# ---------- ADD (FSM) ----------
class AddUserFSM(StatesGroup):
    username = State()
    first_name = State()
    last_name = State()
    email = State()
    phone = State()

@router.message(ActionLike("buttons.add_user:user:admin"))
async def add_user_start(message: Message, current_user: UserRead, state: FSMContext):
    if not _is_admin(current_user):
        return
    lz: Localizer = await get_localizer_by_user(current_user)
    svc = UserService()
    current_user = await svc.change_ui_mode(current_user, UiMode.NEW_USER)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)  # shows Skip/Cancel in NEW_USER
    await state.set_state(AddUserFSM.username)
    await message.answer(lz.get("users.add.step_username"), reply_markup=keyboard)

@router.message(ActionLike("buttons.user:home:admin"))
async def user_start(message: Message, current_user: UserRead, state: FSMContext):
    if not _is_admin(current_user):
        return
    lz: Localizer = await get_localizer_by_user(current_user)
    svc = UserService()
    current_user = await svc.change_ui_mode(current_user, UiMode.USER)
    keyboard = await UserKeyboardFactory().build_for_user(current_user) 
    await message.answer(lz.get("mode.user"), reply_markup=keyboard)

@router.message(ActionLike("buttons.cancel:new_user:admin"))
async def add_user_cancel(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    svc = UserService()
    await state.clear()
    current_user = await svc.change_ui_mode(current_user, UiMode.USER)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("users.common.cancelled"), reply_markup=keyboard)

@router.message(ActionLike("buttons.back:user:admin"))
async def edit_user_back(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    svc = UserService()
    await state.clear()
    current_user = await svc.change_ui_mode(current_user, UiMode.HOME)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("mode.home"), reply_markup=keyboard)

@router.message(ActionLike("buttons.skip:new_user:admin"))
async def add_user_skip(message: Message, current_user: UserRead, state: FSMContext):
    """
    Skip for optional steps (first_name, last_name, email, phone). Username cannot be skipped.
    """
    lz: Localizer = await get_localizer_by_user(current_user)
    svc = UserService()
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    cur = await state.get_state()

    if cur == AddUserFSM.username.state:
        await message.answer(lz.get("users.add.cannot_skip_username"), reply_markup=keyboard)
        return

    if cur == AddUserFSM.first_name.state:
        await state.update_data(first_name=None)
        await state.set_state(AddUserFSM.last_name)
        await message.answer(lz.get("users.add.step_last"), reply_markup=keyboard)
        return

    if cur == AddUserFSM.last_name.state:
        await state.update_data(last_name=None)
        await state.set_state(AddUserFSM.email)
        await message.answer(lz.get("users.add.step_email"), reply_markup=keyboard)
        return

    if cur == AddUserFSM.email.state:
        await state.update_data(email=None)
        await state.set_state(AddUserFSM.phone)
        await message.answer(lz.get("users.add.step_phone"), reply_markup=keyboard)
        return

    if cur == AddUserFSM.phone.state:
        # finalize with phone_number=None
        data = await state.get_data()
        await state.clear()
        created = await svc.create_user(UserCreate(
            tg_username=data["tg_username"],
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            email=data.get("email"),
            phone_number=None
        ))
        current_user = await svc.change_ui_mode(current_user, UiMode.USER)
        keyboard = await UserKeyboardFactory().build_for_user(current_user)
        await message.answer(lz.get("users.add.done", username=f"@{created.tg_username}" if created.tg_username else "—"),
                             reply_markup=keyboard)

@router.message(AddUserFSM.username, F.text)
async def add_user__username(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    raw = message.text.strip()
    if not re.fullmatch(r"@[\w\d_]{3,}", raw):
        keyboard = await UserKeyboardFactory().build_for_user(current_user)
        await message.answer(lz.get("users.add.bad_username"), reply_markup=keyboard)
        return
    await state.update_data(tg_username=_norm_username(raw))
    await state.set_state(AddUserFSM.first_name)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("users.add.step_first"), reply_markup=keyboard)

@router.message(AddUserFSM.first_name, F.text)
async def add_user__first(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    v = message.text.strip()
    await state.update_data(first_name=None if v == "-" else v)
    await state.set_state(AddUserFSM.last_name)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("users.add.step_last"), reply_markup=keyboard)

@router.message(AddUserFSM.last_name, F.text)
async def add_user__last(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    v = message.text.strip()
    await state.update_data(last_name=None if v == "-" else v)
    await state.set_state(AddUserFSM.email)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("users.add.step_email"), reply_markup=keyboard)

@router.message(AddUserFSM.email, F.text)
async def add_user__email(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    v = message.text.strip()
    if v != "-" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
        keyboard = await UserKeyboardFactory().build_for_user(current_user)
        await message.answer(lz.get("users.add.bad_email"), reply_markup=keyboard)
        return
    await state.update_data(email=None if v == "-" else v)
    await state.set_state(AddUserFSM.phone)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("users.add.step_phone"), reply_markup=keyboard)

@router.message(AddUserFSM.phone, F.text)
async def add_user__phone(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    v = message.text.strip()
    await state.update_data(phone_number=None if v == "-" else v)

    data = await state.get_data()
    await state.clear()

    svc = UserService()
    created = await svc.create_user(UserCreate(
        tg_username=data["tg_username"],
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
        email=data.get("email"),
        phone_number=data.get("phone_number"),
    ))
    current_user = await svc.change_ui_mode(current_user, UiMode.USER)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("users.add.done", username=f"@{created.tg_username}" if created.tg_username else "—"),
                         reply_markup=keyboard)

# ---------- EDIT (pick via KB or @username) ----------
class EditUserFSM(StatesGroup):
    waiting_target = State()
    choose_field = State()
    set_value = State()

@router.message(ActionLike("buttons.edit_user:user:admin"))
async def edit_user_start(message: Message, current_user: UserRead, state: FSMContext):
    if not _is_admin(current_user):
        return
    svc = UserService()
    current_user = await svc.change_ui_mode(current_user, UiMode.EDIT_USER)
    lz: Localizer = await get_localizer_by_user(current_user)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)

    await state.set_state(EditUserFSM.waiting_target)
    await _open_page(message, svc, page=0, lz=lz)
    await message.answer(lz.get("users.edit.or_send_username"), reply_markup=keyboard)

@router.message(ActionLike("buttons.cancel:edit_user:admin"))
async def edit_user_cancel(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    svc = UserService()
    await state.clear()
    current_user = await svc.change_ui_mode(current_user, UiMode.USER)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("users.common.cancelled"), reply_markup=keyboard)

@router.message(ActionLike("buttons.back:edit_user:admin"))
async def edit_user_back(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    svc = UserService()
    await state.clear()
    current_user = await svc.change_ui_mode(current_user, UiMode.USER)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("mode.user"), reply_markup=keyboard)

@router.callback_query(F.data.startswith("users.page:"), EditUserFSM.waiting_target)
async def edit_user_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    page = int(cq.data.split(":")[1])
    await _open_page(cq, UserService(), page=page, lz=lz)

@router.callback_query(F.data == "users.cancel")
async def edit_user_cancel_inline(cq: CallbackQuery, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    svc = UserService()
    await state.clear()
    current_user = await svc.change_ui_mode(current_user, UiMode.USER)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await cq.answer(lz.get("users.common.cancelled"))
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.message.answer(lz.get("users.common.cancelled"), reply_markup=keyboard)

@router.callback_query(F.data.startswith("users.pick:"), EditUserFSM.waiting_target)
async def edit_user_pick(cq: CallbackQuery, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    user_id = uuid.UUID(cq.data.split(":")[1])
    svc = UserService()
    target = await svc.get_user(uid=user_id, autoupdate=False)
    if target is None:
        await cq.answer(lz.get("users.edit.not_found"), show_alert=True)
        await _open_page(cq, svc, page=0, lz=lz)
        return

    payload: dict[str, str] = {"target_id": str(user_id)}
    if target.tg_username:
        payload["target_username"] = target.tg_username
    await state.update_data(**payload)
    await state.set_state(EditUserFSM.choose_field)
    await cq.answer()
    choose_text = f"{lz.get('users.edit.choose_field')}\n\n{_format_user_snapshot(target, lz)}"
    await cq.message.edit_text(choose_text, reply_markup=_kb_edit_fields(lz))

@router.message(EditUserFSM.waiting_target, F.text.regexp(r"^@[\w\d_]{3,}$"))
async def edit_user_by_username(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    svc = UserService()
    normalized = _norm_username(message.text)
    target = await svc.get_user(tg_username=normalized, autoupdate=False)
    if target is None:
        await message.answer(lz.get("users.edit.not_found"))
        return

    payload = {"target_id": str(target.id)}
    if target.tg_username:
        payload["target_username"] = target.tg_username
    await state.update_data(**payload)
    await state.set_state(EditUserFSM.choose_field)
    text = f"{lz.get('users.edit.user_selected')}\n\n{_format_user_snapshot(target, lz)}"
    await message.answer(text, reply_markup=_kb_edit_fields(lz))

def _kb_edit_fields(lz: Localizer) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=lz.get("users.fields.first_name"), callback_data="edit.field:first_name")],
        [InlineKeyboardButton(text=lz.get("users.fields.last_name"), callback_data="edit.field:last_name")],
        [InlineKeyboardButton(text=lz.get("users.fields.middle_name"), callback_data="edit.field:middle_name")],
        [InlineKeyboardButton(text=lz.get("users.fields.username"), callback_data="edit.field:tg_username")],
        [InlineKeyboardButton(text=lz.get("users.fields.email"), callback_data="edit.field:email")],
        [InlineKeyboardButton(text=lz.get("users.fields.phone"), callback_data="edit.field:phone_number")],
        [InlineKeyboardButton(text=lz.get("users.fields.role"), callback_data="edit.field:role")],
        [InlineKeyboardButton(text=lz.get("users.nav.back"), callback_data="edit.field:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data.startswith("edit.field:"), EditUserFSM.choose_field)
async def edit_user_choose_field(cq: CallbackQuery, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    field = cq.data.split(":")[1]
    svc = UserService()
    if field == "back":
        await state.set_state(EditUserFSM.waiting_target)
        await cq.answer()
        await _open_page(cq, svc, page=0, lz=lz)
        return

    data = await state.get_data()
    target: Optional[UserRead] = None
    if "target_id" in data:
        target = await svc.get_user(uid=uuid.UUID(data["target_id"]), autoupdate=False)
    elif "target_username" in data:
        target = await svc.get_user(tg_username=data["target_username"], autoupdate=False)

    if target is None:
        await state.clear()
        current_user = await svc.change_ui_mode(current_user, UiMode.USER)
        keyboard = await UserKeyboardFactory().build_for_user(current_user)
        await cq.answer(lz.get("users.edit.not_found"), show_alert=True)
        await cq.message.edit_text(lz.get("users.edit.not_found"), reply_markup=None)
        await cq.message.answer(lz.get("users.common.cancelled"), reply_markup=keyboard)
        return

    snapshot = _format_user_snapshot(target, lz)
    await state.update_data(field=field)
    await state.set_state(EditUserFSM.set_value)

    prompt_key = "users.edit.send_value"
    reply_markup: Optional[InlineKeyboardMarkup] = None

    if field == "role":
        prompt_text = lz.get("users.edit.send_role")
        reply_markup = _kb_role_options(lz)
    elif field == "tg_username":
        prompt_key = "users.edit.send_username"
        prompt_text = lz.get(prompt_key)
    else:
        prompt_text = lz.get(prompt_key)

    await cq.answer()
    await cq.message.edit_text(f"{prompt_text}\n\n{snapshot}", reply_markup=reply_markup)

@router.message(EditUserFSM.set_value, F.text)
async def edit_user_apply(message: Message, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    raw = message.text.strip()

    data = await state.get_data()
    field = data["field"]

    svc = UserService()
    target: Optional[UserRead] = None
    if "target_id" in data:
        target = await svc.get_user(uid=uuid.UUID(data["target_id"]), autoupdate=False)
    elif "target_username" in data:
        target = await svc.get_user(tg_username=data["target_username"], autoupdate=False)

    if target is None:
        await state.clear()
        current_user = await svc.change_ui_mode(current_user, UiMode.USER)
        keyboard = await UserKeyboardFactory().build_for_user(current_user)
        await message.answer(lz.get("users.edit.not_found"), reply_markup=keyboard)
        return

    upd = {"id": target.id}

    if field == "role":
        v = raw.lower()
        if v not in ROLE_VALUES:
            await message.answer(lz.get("users.edit.bad_role"))
            return
        upd["role"] = ROLE_VALUES[v]

    elif field == "tg_username":
        if not re.fullmatch(r"@[\w\d_]{3,}", raw):
            await message.answer(lz.get("users.edit.bad_username"))
            return
        upd["tg_username"] = _norm_username(raw)

    elif field in {"first_name", "last_name", "middle_name"}:
        upd[field] = None if raw == "-" else raw

    elif field == "email":
        if raw != "-" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", raw):
            await message.answer(lz.get("users.edit.bad_email"))
            return
        upd["email"] = None if raw == "-" else raw

    elif field == "phone_number":
        upd["phone_number"] = None if raw == "-" else raw

    else:
        await message.answer(lz.get("users.edit.unsupported"))
        return

    updated = await svc.update_user(UserUpdate(**upd))

    if field == "role" and updated.id == current_user.id and updated.role != UserRole.ADMIN:
        await state.clear()
        current_user = await svc.change_ui_mode(updated, UiMode.HOME)
        keyboard = await UserKeyboardFactory().build_for_user(current_user)
        summary = _format_user_snapshot(updated, lz)
        text = lz.get(
            "users.edit.updated",
            username=f"@{updated.tg_username}" if updated.tg_username else "—",
            field=field,
        )
        composed = f"{text}\n\n{summary}\n\n{lz.get('mode.home')}"
        await message.answer(composed, reply_markup=keyboard)
        return

    await state.set_state(EditUserFSM.choose_field)
    updated_text = lz.get(
        "users.edit.updated",
        username=f"@{updated.tg_username}" if updated.tg_username else "—",
        field=field
    )
    summary = _format_user_snapshot(updated, lz)
    await message.answer(f"{updated_text}\n\n{summary}", reply_markup=_kb_edit_fields(lz))

@router.callback_query(F.data.startswith("edit.role:"), EditUserFSM.set_value)
async def edit_user_apply_role(cq: CallbackQuery, current_user: UserRead, state: FSMContext):
    lz: Localizer = await get_localizer_by_user(current_user)
    data = await state.get_data()
    if data.get("field") != "role":
        await cq.answer()
        return

    parts = cq.data.split(":")
    if len(parts) < 2:
        await cq.answer()
        return

    action = parts[1]
    svc = UserService()

    if action == "back":
        target: Optional[UserRead] = None
        if "target_id" in data:
            target = await svc.get_user(uid=uuid.UUID(data["target_id"]), autoupdate=False)
        elif "target_username" in data:
            target = await svc.get_user(tg_username=data["target_username"], autoupdate=False)

        if target is None:
            await state.clear()
            current_user = await svc.change_ui_mode(current_user, UiMode.USER)
            keyboard = await UserKeyboardFactory().build_for_user(current_user)
            await cq.answer(lz.get("users.edit.not_found"), show_alert=True)
            await cq.message.edit_reply_markup(reply_markup=None)
            await cq.message.answer(lz.get("users.edit.not_found"), reply_markup=keyboard)
            return

        await state.set_state(EditUserFSM.choose_field)
        await cq.answer()
        choose_text = f"{lz.get('users.edit.choose_field')}\n\n{_format_user_snapshot(target, lz)}"
        await cq.message.edit_text(choose_text, reply_markup=_kb_edit_fields(lz))
        return

    if action != "set" or len(parts) < 3:
        await cq.answer()
        return

    role_key = parts[2]
    if role_key not in ROLE_VALUES:
        await cq.answer(lz.get("users.edit.bad_role"), show_alert=True)
        return

    target: Optional[UserRead] = None
    if "target_id" in data:
        target = await svc.get_user(uid=uuid.UUID(data["target_id"]), autoupdate=False)
    elif "target_username" in data:
        target = await svc.get_user(tg_username=data["target_username"], autoupdate=False)

    if target is None:
        await state.clear()
        current_user = await svc.change_ui_mode(current_user, UiMode.USER)
        keyboard = await UserKeyboardFactory().build_for_user(current_user)
        await cq.answer()
        await cq.message.edit_reply_markup(reply_markup=None)
        await cq.message.answer(lz.get("users.edit.not_found"), reply_markup=keyboard)
        return

    updated = await svc.update_user(UserUpdate(id=target.id, role=ROLE_VALUES[role_key]))
    await cq.message.edit_reply_markup(reply_markup=None)

    if updated.id == current_user.id and updated.role != UserRole.ADMIN:
        await state.clear()
        current_user = await svc.change_ui_mode(updated, UiMode.HOME)
        keyboard = await UserKeyboardFactory().build_for_user(current_user)
        update_text = lz.get(
            "users.edit.updated",
            username=f"@{updated.tg_username}" if updated.tg_username else "—",
            field="role",
        )
        summary = _format_user_snapshot(updated, lz)
        await cq.message.edit_text(f"{update_text}\n\n{summary}", reply_markup=None)
        await cq.answer()
        await cq.message.answer(lz.get("mode.home"), reply_markup=keyboard)
        return

    await state.set_state(EditUserFSM.choose_field)
    await cq.answer()
    update_text = lz.get(
        "users.edit.updated",
        username=f"@{updated.tg_username}" if updated.tg_username else "—",
        field="role",
    )
    summary = _format_user_snapshot(updated, lz)
    await cq.message.edit_text(f"{update_text}\n\n{summary}", reply_markup=_kb_edit_fields(lz))


instrument_router_module(globals(), prefix="users")
