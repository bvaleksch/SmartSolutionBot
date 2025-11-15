# bot/routers/core.py
import uuid
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from typing import Optional
from smart_solution.db.schemas.user import UserRead, UserUpdate
from smart_solution.i18n import lang_code2language, Localizer
from smart_solution.bot.services.language import LanguageService
from smart_solution.bot.services.user import UserService
from smart_solution.bot.services.team import TeamService
from smart_solution.bot.services.competition import CompetitionService
from smart_solution.bot.keyboards.user_keyboard_factory import UserKeyboardFactory
from smart_solution.db.enums import UiMode, UserRole
from smart_solution.bot.filters.action_like import ActionLike
from smart_solution.bot.services.audit_log import instrument_router_module

router = Router(name="core")

async def get_localizer_by_user(user: UserRead) -> Localizer:
    lng_svc = LanguageService()
    lang = await lng_svc.safe_autoget(user.preferred_language_id)
    localizer = Localizer(lang.name)
    return localizer

async def autoset_language(message: Message, current_user: UserRead) -> None:
    if current_user.preferred_language_id is not None:
        return 

    lng_svc = LanguageService()
    usr_svc = UserService()
    lang = lang_code2language(message.from_user.language_code)
    lang = await lng_svc.safe_autoget(lang)
    usr = UserUpdate(id=current_user.id, preferred_language_id=lang.id)
    await usr_svc.update_user(usr)
    current_user.preferred_language_id = lang.id

@router.message(Command("switch_role"))
async def switch_role(message: Message, current_user: UserRead, is_whitelisted: bool) -> None:
    if not is_whitelisted:
        return

    localizer = await get_localizer_by_user(current_user)
    buttons = []
    for role in ["admin", "contestant", "unregistered"]:
        buttons.append([InlineKeyboardButton(text=role, callback_data="role_set " + role)])
    inline_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(text=localizer.get("core.choose_role"), reply_markup=inline_keyboard)

@router.callback_query(F.data.startswith("role_set "))
async def on_role_set(cq: CallbackQuery, current_user: UserRead) -> None:
    localizer = await get_localizer_by_user(current_user)
    usr_svc = UserService()

    role_name = cq.data.split()[-1]
    match role_name:
        case "admin":
            role = UserRole.ADMIN
        case "contestant":
            role = UserRole.CONTESTANT
        case _:
            role = UserRole.UNREGISTERED

    current_user = await usr_svc.change_role(current_user, role)
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.HOME)

    await cq.answer(localizer.get("core.role_changed"))
    await cq.message.answer(text=localizer.get("core.role_changed"), reply_markup=(await UserKeyboardFactory().build_for_user(current_user)))

@router.message(ActionLike("buttons.change_language:*:*"))
async def open_change_language(message: Message, current_user: UserRead) -> None:
    lng_svc = LanguageService()
    usr_svc = UserService()
    localizer = await get_localizer_by_user(current_user)
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.CHANGE_LANGUAGE)

    languages = await lng_svc.all_languages()
    buttons = []
    for lang in languages:
        buttons.append([InlineKeyboardButton(text=lang.title, callback_data="lang_set " + str(lang.id))])
    inline_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(text=localizer.get("core.choose_language"), reply_markup=inline_keyboard)
    await message.answer(text=localizer.get("core.you_can_also_go_back"), reply_markup=(await UserKeyboardFactory().build_for_user(current_user)))

async def render_profile_message(user: UserRead):
    localizer = await get_localizer_by_user(user)
    team_svc = TeamService()
    cmpt_svc = CompetitionService()

    def get_full_name():
        name = user.first_name
        if user.first_name is None:
            return None
        if user.last_name is not None:
            name += " " + user.last_name

        return name

    name = get_full_name()
    name = name if name is not None else localizer.get("core.your_name")
    role = localizer.get(f"roles.{str(user.role).lower()}")
    email = str(user.email)
    phone = user.phone_number
    if not (await team_svc.has_selected_team(user)):
        text = localizer.get("profile.without_team", username=user.tg_username, name=name, role=role, email=email, phone=phone)
        return text

    team = await team_svc.get_selected_team_by_user(user)
    short_track_info = await cmpt_svc.get_short_track_info(team.track_id)
    cmpt_title = short_track_info.competition_title
    team_name = team.title
    track_title = short_track_info.track_title
    text = localizer.get("profile.with_team", username=user.tg_username, name=name, role=role, email=email, phone=phone, competition=cmpt_title, team_name=team_name, team_track=track_title)

    return text

@router.message(ActionLike("buttons.profile:*:*"))
async def show_profile(message: Message, current_user: UserRead) -> None:
    await message.answer(text=await render_profile_message(current_user), reply_markup=(await UserKeyboardFactory().build_for_user(current_user)))

@router.message(ActionLike("buttons.back:change_language:*"))
async def on_back_from_change_language(message: Message, current_user: UserRead) -> None:
    usr_svc = UserService()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.HOME)
    localizer = await get_localizer_by_user(current_user)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(text=localizer.get("core.exit_language_change_mode"), reply_markup=keyboard)

@router.callback_query(F.data.startswith("lang_set "))
async def on_lang_set(cq: CallbackQuery, current_user: UserRead) -> None:
    localizer = await get_localizer_by_user(current_user)
    if current_user.ui_mode != UiMode.CHANGE_LANGUAGE:
        await cq.answer(localizer.get("core.exit_language_change_mode"))
        return

    lang_uuid = uuid.UUID(cq.data.split()[-1])
    lng_svc = LanguageService()
    usr_svc = UserService()
    lang = await lng_svc.safe_autoget(lang_uuid)
    current_user = await usr_svc.change_language(current_user, lang.id)
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.HOME)
    localizer = await get_localizer_by_user(current_user)

    await cq.answer(localizer.get("core.language_changed"))
    await cq.message.answer(text=localizer.get("core.language_changed"), reply_markup=(await UserKeyboardFactory().build_for_user(current_user)))

@router.message(ActionLike("buttons.help:*:*"))
async def help(message: Message, current_user: UserRead) -> None:
    usr_svc = UserService()
    localizer = await get_localizer_by_user(current_user)

    text = localizer.get(f"help.{str(current_user.role).lower()}")

    await message.answer(text=text, reply_markup=(await UserKeyboardFactory().build_for_user(current_user)))

@router.message(CommandStart())
async def start(message: Message, current_user: UserRead, is_whitelisted: bool) -> None:
    await autoset_language(message, current_user)
    usr_svc = UserService()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.HOME)
    localizer = await get_localizer_by_user(current_user)
    
    first_name = current_user.first_name
    first_name = first_name if first_name is not None else message.from_user.full_name
    first_name = first_name if first_name != "" else localizer.get("start.your_name")
    
    if not is_whitelisted:
        greeting_text = localizer.get(f"start.{str(current_user.role).lower()}", first_name=first_name)
    else:
        greeting_text = localizer.get(f"start.whitelist", first_name=first_name)

    await message.answer(greeting_text, reply_markup=(await UserKeyboardFactory().build_for_user(current_user)))


instrument_router_module(globals(), prefix="core")
