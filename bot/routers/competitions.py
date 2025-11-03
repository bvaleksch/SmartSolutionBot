# bot/routers/competitions.py
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from zoneinfo import ZoneInfo

from aiogram import Router, F, Bot
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Document,
)

from smart_solution.i18n import Localizer
from smart_solution.db.enums import UiMode, UserRole, PageType, SortDirection
from smart_solution.db.schemas.user import UserRead
from smart_solution.db.schemas.competition import (
    CompetitionCreate,
    CompetitionUpdate,
    CompetitionRead,
)
from smart_solution.db.schemas.track import TrackCreate, TrackRead
from smart_solution.db.schemas.page import PageCreate
from smart_solution.bot.filters.action_like import ActionLike
from smart_solution.bot.keyboards.user_keyboard_factory import UserKeyboardFactory
from smart_solution.bot.services.competition import CompetitionService
from smart_solution.bot.services.language import LanguageService
from smart_solution.bot.services.page import PageService
from smart_solution.bot.services.user import UserService
from smart_solution.bot.routers.utils import get_localizer_by_user

router = Router(name="competitions_admin")
DATETIME_FMT = "%Y-%m-%d %H:%M"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC_TZ = ZoneInfo("UTC")
MOSCOW_LABEL = "MSK"
PAGE_SIZE = 6
CONTENT_ROOT = Path(__file__).resolve().parents[2] / "data" / "content"


# ---------- helpers ----------
def _is_admin(user: UserRead) -> bool:
    return str(user.role).lower() == UserRole.ADMIN


def _normalize_slug(value: str) -> str:
    return value.strip().lower()


def _valid_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_]{3,}", value))


async def _competition_snapshot(
    comp: CompetitionRead, svc: CompetitionService, lz: Localizer
) -> str:
    tracks: List[TrackRead] = await svc.list_tracks(comp.id)
    start_at = _format_datetime_moscow(comp.start_at)
    end_at = _format_datetime_moscow(comp.end_at)

    lines = [
        f"• {lz.get('competitions.fields.title')}: {comp.title}",
        f"• {lz.get('competitions.fields.slug')}: {comp.slug}",
        f"• {lz.get('competitions.fields.start_at')}: {start_at}",
        f"• {lz.get('competitions.fields.end_at')}: {end_at}",
        f"• {lz.get('competitions.track.count')}: {len(tracks)}",
    ]

    if tracks:
        track_lines = [
            lz.get(
                "competitions.track.item",
                title=t.title,
                slug=t.slug,
                max_contestants=f"{t.max_contestants}",
                max_submissions=f"{t.max_submissions_total}",
            )
            for t in tracks
        ]
        lines.append(lz.get("competitions.track.list_header"))
        lines.extend(track_lines)
    else:
        lines.append(lz.get("competitions.track.empty"))

    return lz.get("competitions.edit.current_info", details="\n".join(lines))


def _kb_competitions_page(
    comps: List[CompetitionRead], page: int, pages: int, lz: Localizer
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for comp in comps:
        label = lz.get(
            "competitions.item",
            title=comp.title,
            start=_format_datetime_moscow(comp.start_at),
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"competitions.pick:{comp.id}",
                )
            ]
        )

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text=lz.get("competitions.nav.prev"),
                callback_data=f"competitions.page:{page-1}",
            )
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                text=lz.get("competitions.nav.next"),
                callback_data=f"competitions.page:{page+1}",
            )
        )

    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text=lz.get("buttons.cancel"), callback_data="competitions.cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _open_competitions_page(
    target: Message | CallbackQuery,
    svc: CompetitionService,
    page: int,
    lz: Localizer,
) -> None:
    comps, total = await svc.list_competitions_page(page=page, page_size=PAGE_SIZE)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    kb = _kb_competitions_page(comps, page, pages, lz)
    text = lz.get("competitions.edit.pick_competition", page=f"{page+1}", pages=f"{pages}")

    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()


def _kb_edit_fields(lz: Localizer) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=lz.get("competitions.fields.title"), callback_data="comp.field:title")],
        [InlineKeyboardButton(text=lz.get("competitions.fields.slug"), callback_data="comp.field:slug")],
        [InlineKeyboardButton(text=lz.get("competitions.fields.start_at"), callback_data="comp.field:start_at")],
        [InlineKeyboardButton(text=lz.get("competitions.fields.end_at"), callback_data="comp.field:end_at")],
        [InlineKeyboardButton(text=lz.get("competitions.fields.back"), callback_data="comp.field:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(UTC_TZ)


def _format_datetime_moscow(dt: datetime) -> str:
    aware_utc = _ensure_utc(dt)
    local_dt = aware_utc.astimezone(MOSCOW_TZ)
    return f"{local_dt.strftime(DATETIME_FMT)} {MOSCOW_LABEL}"


def _parse_datetime(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    try:
        local_dt = datetime.strptime(raw, DATETIME_FMT)
    except ValueError:
        return None
    aware_local = local_dt.replace(tzinfo=MOSCOW_TZ)
    return aware_local.astimezone(UTC_TZ).replace(tzinfo=None)


async def _ensure_competition(
    comp_id_str: Optional[str], svc: CompetitionService
) -> Optional[CompetitionRead]:
    if not comp_id_str:
        return None
    try:
        comp_id = uuid.UUID(comp_id_str)
    except ValueError:
        return None
    return await svc.get_competition_by_id(comp_id)


# ---------- states ----------
class AddCompetitionFSM(StatesGroup):
    title = State()
    slug = State()
    start_at = State()
    end_at = State()


class EditCompetitionFSM(StatesGroup):
    waiting_target = State()
    choose_field = State()
    set_value = State()


class TrackCreateFSM(StatesGroup):
    title = State()
    slug = State()
    max_contestants = State()
    max_submissions = State()
    sort_by = State()
    about = State()
    rule = State()
    instruction = State()


# ---------- mode entry ----------
@router.message(ActionLike("buttons.competition:*:admin"))
async def competition_mode(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.COMPETITION)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("competitions.mode.enter"), reply_markup=keyboard)


@router.message(ActionLike("buttons.back:competition:admin"))
async def competition_back(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return
    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.HOME)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("mode.home"), reply_markup=keyboard)


# ---------- add competition ----------
@router.message(ActionLike("buttons.add_competition:competition:admin"))
async def add_competition_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.NEW_COMPETITION)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await state.set_state(AddCompetitionFSM.title)
    await message.answer(lz.get("competitions.add.step_title"), reply_markup=keyboard)


@router.message(ActionLike("buttons.cancel:new_competition:admin"))
async def add_competition_cancel(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return
    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.COMPETITION)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("competitions.common.cancelled"), reply_markup=keyboard)


@router.message(ActionLike("buttons.add_track:new_competition:admin"))
async def add_track_blocked(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return
    current = await state.get_state()
    if current not in {AddCompetitionFSM.title.state, AddCompetitionFSM.slug.state,
                       AddCompetitionFSM.start_at.state, AddCompetitionFSM.end_at.state}:
        return
    lz = await get_localizer_by_user(current_user)
    await message.answer(lz.get("competitions.add.finish_fields_first"))


@router.message(AddCompetitionFSM.title, F.text)
async def add_competition_title(message: Message, current_user: UserRead, state: FSMContext) -> None:
    title = message.text.strip()
    if not title:
        lz = await get_localizer_by_user(current_user)
        await message.answer(lz.get("competitions.add.step_title"))
        return

    await state.update_data(title=title)
    await state.set_state(AddCompetitionFSM.slug)
    lz = await get_localizer_by_user(current_user)
    await message.answer(lz.get("competitions.add.step_slug"))


@router.message(AddCompetitionFSM.slug, F.text)
async def add_competition_slug(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    raw = _normalize_slug(message.text)
    if not _valid_slug(raw):
        await message.answer(lz.get("competitions.add.bad_slug"))
        return

    comp_svc = CompetitionService()
    existing = await comp_svc.get_competition(raw)
    if existing is not None:
        await message.answer(lz.get("competitions.add.slug_exists"))
        return

    await state.update_data(slug=raw)
    await state.set_state(AddCompetitionFSM.start_at)
    await message.answer(lz.get("competitions.add.step_start_at", fmt=DATETIME_FMT))


@router.message(AddCompetitionFSM.start_at, F.text)
async def add_competition_start_at(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    dt = _parse_datetime(message.text)
    if dt is None:
        await message.answer(lz.get("competitions.add.bad_datetime", fmt=DATETIME_FMT))
        return

    await state.update_data(start_at=dt)
    await state.set_state(AddCompetitionFSM.end_at)
    await message.answer(lz.get("competitions.add.step_end_at", fmt=DATETIME_FMT))


@router.message(AddCompetitionFSM.end_at, F.text)
async def add_competition_end_at(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    start_at: datetime = data["start_at"]
    end_at = _parse_datetime(message.text)
    if end_at is None:
        await message.answer(lz.get("competitions.add.bad_datetime", fmt=DATETIME_FMT))
        return
    if end_at <= start_at:
        await message.answer(lz.get("competitions.add.end_before_start"))
        return

    comp_svc = CompetitionService()
    new_comp = await comp_svc.create_competition(
        CompetitionCreate(
            title=data["title"],
            slug=data["slug"],
            start_at=start_at,
            end_at=end_at,
        )
    )

    usr_svc = UserService()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.EDIT_COMPETITION)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)

    await state.clear()
    await state.update_data(
        target_competition_id=str(new_comp.id),
        target_competition_slug=new_comp.slug,
        target_competition_title=new_comp.title,
        tracks_created=0,
        require_track=True,
        track_title=None,
        track_slug=None,
        track_max_contestants=None,
        track_max_submissions=None,
        track_about_files={},
        track_rule_files={},
        track_instruction_files={},
        page_languages=None,
        page_lang_index=0,
    )
    await state.set_state(TrackCreateFSM.title)

    created_text = lz.get("competitions.add.created", title=new_comp.title)
    await message.answer(created_text, reply_markup=keyboard)
    await message.answer(lz.get("competitions.add.need_track"))
    await message.answer(lz.get("competitions.track.step_title"))


# ---------- track creation ----------
@router.message(TrackCreateFSM.title, F.text)
async def track_title(message: Message, current_user: UserRead, state: FSMContext) -> None:
    title = message.text.strip()
    if not title:
        lz = await get_localizer_by_user(current_user)
        await message.answer(lz.get("competitions.track.step_title"))
        return

    await state.update_data(track_title=title)
    await state.set_state(TrackCreateFSM.slug)
    lz = await get_localizer_by_user(current_user)
    await message.answer(lz.get("competitions.track.step_slug"))


@router.message(TrackCreateFSM.slug, F.text)
async def track_slug(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    raw = _normalize_slug(message.text)
    if not _valid_slug(raw):
        await message.answer(lz.get("competitions.track.bad_slug"))
        return

    comp_svc = CompetitionService()
    data = await state.get_data()
    comp = await _ensure_competition(data.get("target_competition_id"), comp_svc)
    if comp is None:
        await message.answer(lz.get("competitions.edit.not_found"))
        await state.clear()
        return

    tracks = await comp_svc.list_tracks(comp.id)
    if any(tr.slug.lower() == raw for tr in tracks):
        await message.answer(lz.get("competitions.track.slug_exists"))
        return

    await state.update_data(track_slug=raw)
    await state.set_state(TrackCreateFSM.max_contestants)
    await message.answer(lz.get("competitions.track.step_max_contestants"))


def _parse_positive_int(raw: str) -> Optional[int]:
    raw = raw.strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


def _lang_key(name: str) -> str:
    return name.strip().lower()


def _current_language_info(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    languages = data.get("page_languages") or []
    index = data.get("page_lang_index", 0)
    try:
        return languages[index]
    except (IndexError, TypeError):
        return None


def _ensure_content_dir(language_name: str, comp_slug: str, track_slug: str) -> Path:
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
    lang_segment = _lang_key(language_name or "") or "unknown"
    track_dir = CONTENT_ROOT / lang_segment / comp_slug / track_slug
    track_dir.mkdir(parents=True, exist_ok=True)
    return track_dir


async def _save_html_document(bot: Bot, document: Document, language_name: str, comp_slug: str, track_slug: str, kind: str) -> str:
    original_name = document.file_name or ""
    ext = Path(original_name).suffix.lower()
    if ext not in {".html", ".htm"}:
        ext = ".html"

    filename = f"{kind}{ext}"
    dest_dir = _ensure_content_dir(language_name, comp_slug, track_slug)
    dest_path = dest_dir / filename
    file = await bot.get_file(document.file_id)
    await bot.download(file, destination=str(dest_path))
    return dest_path.relative_to(CONTENT_ROOT).as_posix()


@router.message(TrackCreateFSM.max_contestants, F.text)
async def track_max_contestants(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    raw = message.text.strip()
    if raw in {"", "-"}:
        await state.update_data(track_max_contestants=3)
        await state.set_state(TrackCreateFSM.max_submissions)
        await message.answer(lz.get("competitions.track.step_max_submissions"))
        return

    value = _parse_positive_int(raw)
    if value is None:
        await message.answer(lz.get("competitions.track.bad_number"))
        return

    await state.update_data(track_max_contestants=value)
    await state.set_state(TrackCreateFSM.max_submissions)
    await message.answer(lz.get("competitions.track.step_max_submissions"))



@router.message(TrackCreateFSM.sort_by, F.text)
async def track_sort_by(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    choice = (message.text or "").strip().lower()
    mapping = {
        "asc": SortDirection.ASC,
        "desc": SortDirection.DESC,
        "ascending": SortDirection.ASC,
        "descending": SortDirection.DESC,
        "возрастание": SortDirection.ASC,
        "убывание": SortDirection.DESC,
    }
    direction = mapping.get(choice)
    if direction is None:
        await message.answer(lz.get("competitions.track.bad_sort_by"))
        return

    await state.update_data(track_sort_by=direction.value)
    data = await state.get_data()
    languages = data.get("page_languages") or []
    index = int(data.get("page_lang_index", 0) or 0)
    current_lang = ""
    if languages:
        lang_entry = languages[index if 0 <= index < len(languages) else 0]
        current_lang = lang_entry.get("title") or lang_entry.get("name") or ""
    await state.set_state(TrackCreateFSM.about)
    if current_lang:
        await message.answer(lz.get("competitions.track.step_about", language=current_lang))
    else:
        await message.answer(lz.get("competitions.track.step_about", language=""))

@router.message(TrackCreateFSM.max_submissions, F.text)
async def track_max_submissions(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    value = _parse_positive_int(message.text)
    if value is None:
        await message.answer(lz.get("competitions.track.bad_number"))
        return
    lang_svc = LanguageService()
    languages_list = await lang_svc.all_languages()
    lang_map = {_lang_key(lang.name): lang for lang in languages_list}
    required_keys = ["english", "russian"]
    languages: List[Dict[str, Any]] = []
    missing: List[str] = []
    for key in required_keys:
        lang = lang_map.get(key)
        if not lang:
            missing.append(key)
            continue
        languages.append(
            {
                "id": str(lang.id),
                "name": lang.name,
                "title": lang.title,
            }
        )
    if missing:
        missing_display = ", ".join(name.capitalize() for name in missing)
        await message.answer(lz.get("competitions.track.missing_language", languages=missing_display))
        return

    await state.update_data(
        track_max_submissions=value,
        page_languages=languages,
        page_lang_index=0,
        track_about_files={},
        track_rule_files={},
        track_instruction_files={},
        track_sort_by=SortDirection.DESC.value,
    )
    await state.set_state(TrackCreateFSM.sort_by)
    await message.answer(lz.get("competitions.track.step_sort_by"))


@router.message(TrackCreateFSM.about, F.document)
async def track_about_file(message: Message, current_user: UserRead, state: FSMContext, bot: Bot) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    comp_slug = data.get("target_competition_slug")
    track_slug = data.get("track_slug")
    lang_info = _current_language_info(data)
    if not comp_slug or not track_slug or not lang_info:
        await message.answer(lz.get("competitions.edit.not_found"))
        await state.clear()
        return

    if message.document is None:
        lang_title = lang_info.get("title") or lang_info.get("name")
        await message.answer(lz.get("competitions.track.expect_html", language=lang_title))
        return

    lang_name = lang_info.get("name")
    lang_title = lang_info.get("title") or lang_name
    stored = await _save_html_document(bot, message.document, lang_name, comp_slug, track_slug, "about")
    about_files = dict(data.get("track_about_files") or {})
    about_files[_lang_key(lang_name)] = stored
    await state.update_data(track_about_files=about_files)
    await state.set_state(TrackCreateFSM.rule)
    await message.answer(lz.get("competitions.track.about_received", language=lang_title))
    await message.answer(lz.get("competitions.track.step_rule", language=lang_title))


@router.message(TrackCreateFSM.about)
async def track_about_expect_html(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    lang_info = _current_language_info(data) or {}
    lang_title = lang_info.get("title") or lang_info.get("name") or ""
    if lang_title:
        await message.answer(lz.get("competitions.track.expect_html", language=lang_title))
    else:
        await message.answer(lz.get("competitions.track.expect_html_generic"))


@router.message(TrackCreateFSM.rule, F.document)
async def track_rule_file(message: Message, current_user: UserRead, state: FSMContext, bot: Bot) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    comp_svc = CompetitionService()
    comp = await _ensure_competition(data.get("target_competition_id"), comp_svc)
    if comp is None:
        await message.answer(lz.get("competitions.edit.not_found"))
        await state.clear()
        return

    comp_slug = data.get("target_competition_slug")
    track_slug = data.get("track_slug")
    track_title = data.get("track_title")
    lang_info = _current_language_info(data)
    if not comp_slug or not track_slug or not track_title or not lang_info:
        await message.answer(lz.get("competitions.edit.not_found"))
        await state.clear()
        return

    lang_name = lang_info.get("name")
    lang_title = lang_info.get("title") or lang_name
    about_files = dict(data.get("track_about_files") or {})
    about_relative = about_files.get(_lang_key(lang_name))
    if not about_relative:
        await message.answer(lz.get("competitions.track.expect_html", language=lang_title))
        return

    if message.document is None:
        await message.answer(lz.get("competitions.track.expect_html", language=lang_title))
        return

    rule_files = dict(data.get("track_rule_files") or {})
    rule_relative = await _save_html_document(bot, message.document, lang_name, comp_slug, track_slug, "rule")
    rule_files[_lang_key(lang_name)] = rule_relative
    await state.update_data(track_rule_files=rule_files, track_about_files=about_files)

    await message.answer(lz.get("competitions.track.rule_received", language=lang_title))
    await state.set_state(TrackCreateFSM.instruction)
    await message.answer(lz.get("competitions.track.step_instruction", language=lang_title))


@router.message(TrackCreateFSM.rule)
async def track_rule_expect_html(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    lang_info = _current_language_info(data) or {}
    lang_title = lang_info.get("title") or lang_info.get("name") or ""
    if lang_title:
        await message.answer(lz.get("competitions.track.expect_html", language=lang_title))
    else:
        await message.answer(lz.get("competitions.track.expect_html_generic"))


@router.message(TrackCreateFSM.instruction, F.document)
async def track_instruction_file(message: Message, current_user: UserRead, state: FSMContext, bot: Bot) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    comp_slug = data.get("target_competition_slug")
    track_slug = data.get("track_slug")
    lang_info = _current_language_info(data)
    if not comp_slug or not track_slug or not lang_info:
        await message.answer(lz.get("competitions.edit.not_found"))
        await state.clear()
        return

    lang_name = lang_info.get("name")
    lang_title = lang_info.get("title") or lang_name
    instruction_files = dict(data.get("track_instruction_files") or {})
    stored = await _save_html_document(bot, message.document, lang_name, comp_slug, track_slug, "instruction")
    instruction_files[_lang_key(lang_name)] = stored
    await state.update_data(track_instruction_files=instruction_files)
    await message.answer(lz.get("competitions.track.instruction_received", language=lang_title))
    await _advance_or_finalize_track(message, current_user, state)


@router.message(TrackCreateFSM.instruction, F.text)
async def track_instruction_text(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    raw = message.text.strip()
    data = await state.get_data()
    lang_info = _current_language_info(data) or {}
    lang_title = lang_info.get("title") or lang_info.get("name") or ""
    if raw not in {"-", "skip", "Skip"}:
        if lang_title:
            await message.answer(lz.get("competitions.track.expect_instruction", language=lang_title))
        else:
            await message.answer(lz.get("competitions.track.expect_instruction_generic"))
        return

    instruction_files = dict(data.get("track_instruction_files") or {})
    await state.update_data(track_instruction_files=instruction_files)
    if lang_title:
        await message.answer(lz.get("competitions.track.instruction_skipped", language=lang_title))
    await _advance_or_finalize_track(message, current_user, state)


async def _advance_or_finalize_track(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    languages = data.get("page_languages") or []
    index = int(data.get("page_lang_index", 0) or 0)
    if index + 1 < len(languages):
        next_index = index + 1
        next_lang = languages[next_index]
        next_title = next_lang.get("title") or next_lang.get("name")
        await state.update_data(page_lang_index=next_index)
        await state.set_state(TrackCreateFSM.about)
        await message.answer(lz.get("competitions.track.step_about", language=next_title))
        return

    await _finalize_track_creation(message, current_user, state)


async def _finalize_track_creation(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    comp_svc = CompetitionService()
    comp = await comp_svc.get_competition_by_id(uuid.UUID(data["target_competition_id"]))
    if comp is None:
        await message.answer(lz.get("competitions.edit.not_found"))
        await state.clear()
        return

    track = await comp_svc.create_track(
        TrackCreate(
            title=data.get("track_title"),
            slug=data.get("track_slug"),
            competition_id=comp.id,
            max_contestants=data.get("track_max_contestants", 3),
            max_submissions_total=data.get("track_max_submissions"),
            sort_by=SortDirection(data.get("track_sort_by", SortDirection.DESC.value)),
        )
    )

    page_svc = PageService()
    about_files = dict(data.get("track_about_files") or {})
    rule_files = dict(data.get("track_rule_files") or {})
    instruction_files = dict(data.get("track_instruction_files") or {})
    languages = data.get("page_languages") or []

    instructions_added = False
    for lang in languages:
        lang_id = uuid.UUID(lang["id"])
        lang_name_entry = lang["name"]
        lang_title_entry = lang.get("title") or lang_name_entry
        key = _lang_key(lang_name_entry)
        about_path = about_files.get(key)
        rule_path = rule_files.get(key)
        if not about_path or not rule_path:
            await message.answer(lz.get("competitions.track.missing_file", language=lang_title_entry))
            return

        about_title = lz.get("competitions.track.about_page_title", title=track.title, language=lang_title_entry)
        rule_title = lz.get("competitions.track.rule_page_title", title=track.title, language=lang_title_entry)

        await page_svc.create_page(
            PageCreate(
                title=about_title,
                slug=f"about_{track.slug}",
                type=PageType.ABOUT,
                file_basename=about_path,
                competition_id=comp.id,
                track_id=track.id,
                language_id=lang_id,
            )
        )
        await page_svc.create_page(
            PageCreate(
                title=rule_title,
                slug=f"rule_{track.slug}",
                type=PageType.RULES,
                file_basename=rule_path,
                competition_id=comp.id,
                track_id=track.id,
                language_id=lang_id,
            )
        )

        instruction_path = instruction_files.get(key)
        if instruction_path:
            instructions_added = True
            instruction_title = lz.get(
                "competitions.track.instruction_page_title",
                title=track.title,
                language=lang_title_entry,
            )
            await page_svc.create_page(
                PageCreate(
                    title=instruction_title,
                    slug=f"instruction_{track.slug}",
                    type=PageType.INSTRUCTION,
                    file_basename=instruction_path,
                    competition_id=comp.id,
                    track_id=track.id,
                    language_id=lang_id,
                )
            )

    tracks_created = int(data.get("tracks_created", 0) or 0) + 1
    updates = {
        "tracks_created": tracks_created,
        "track_title": None,
        "track_slug": None,
        "track_about_files": None,
        "track_rule_files": None,
        "track_instruction_files": None,
        "track_sort_by": None,
        "track_max_contestants": None,
        "track_max_submissions": None,
        "page_languages": None,
        "page_lang_index": None,
    }
    if data.get("require_track") and tracks_created >= 1:
        updates["require_track"] = False
    await state.update_data(**updates)
    await state.set_state(EditCompetitionFSM.choose_field)

    snapshot = await _competition_snapshot(comp, comp_svc, lz)
    header = lz.get("competitions.track.created", title=track.title)
    pages_note = lz.get("competitions.track.pages_created")
    if instructions_added:
        pages_note += "\n" + lz.get("competitions.track.instructions_saved")
    await message.answer(f"{header}\n\n{pages_note}\n\n{snapshot}", reply_markup=_kb_edit_fields(lz))


# ---------- edit competition ----------
@router.message(ActionLike("buttons.edit_competition:competition:admin"))
async def edit_competition_start(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.EDIT_COMPETITION)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await state.set_state(EditCompetitionFSM.waiting_target)

    await _open_competitions_page(message, CompetitionService(), page=0, lz=lz)
    await message.answer(lz.get("competitions.edit.or_send_slug"), reply_markup=keyboard)


@router.message(ActionLike("buttons.cancel:edit_competition:admin"))
async def edit_competition_cancel(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    data = await state.get_data()
    if data.get("require_track") and int(data.get("tracks_created", 0)) == 0:
        lz = await get_localizer_by_user(current_user)
        await message.answer(lz.get("competitions.track.need_one"))
        return

    lz = await get_localizer_by_user(current_user)
    usr_svc = UserService()
    await state.clear()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.COMPETITION)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(lz.get("competitions.common.cancelled"), reply_markup=keyboard)


@router.message(ActionLike("buttons.add_track:edit_competition:admin"))
async def edit_competition_add_track(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if not _is_admin(current_user):
        return

    data = await state.get_data()
    comp_id = data.get("target_competition_id")
    comp_svc = CompetitionService()
    comp = await _ensure_competition(comp_id, comp_svc)
    lz = await get_localizer_by_user(current_user)
    if comp is None:
        await message.answer(lz.get("competitions.errors.select_first"))
        return

    existing_tracks = await comp_svc.list_tracks(comp.id)
    await state.update_data(
        target_competition_id=str(comp.id),
        target_competition_slug=comp.slug,
        target_competition_title=comp.title,
        tracks_created=len(existing_tracks),
        track_title=None,
        track_slug=None,
        track_max_contestants=None,
        track_max_submissions=None,
        track_about_files={},
        track_rule_files={},
        page_languages=None,
        page_lang_index=0,
    )
    await state.set_state(TrackCreateFSM.title)
    await message.answer(lz.get("competitions.track.step_title"))


@router.callback_query(F.data.startswith("competitions.page:"), EditCompetitionFSM.waiting_target)
async def edit_competition_page(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    page = int(cq.data.split(":")[1])
    await _open_competitions_page(cq, CompetitionService(), page=page, lz=lz)


@router.callback_query(F.data == "competitions.cancel", EditCompetitionFSM.waiting_target)
async def edit_competition_cancel_inline(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    await state.clear()
    usr_svc = UserService()
    current_user = await usr_svc.change_ui_mode(current_user, UiMode.COMPETITION)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await cq.answer(lz.get("competitions.common.cancelled"))
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.message.answer(lz.get("competitions.common.cancelled"), reply_markup=keyboard)


@router.callback_query(F.data.startswith("competitions.pick:"), EditCompetitionFSM.waiting_target)
async def edit_competition_pick(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    comp_id = uuid.UUID(cq.data.split(":")[1])
    comp_svc = CompetitionService()
    comp = await comp_svc.get_competition_by_id(comp_id)
    if comp is None:
        await cq.answer(lz.get("competitions.edit.not_found"), show_alert=True)
        return

    tracks = await comp_svc.list_tracks(comp.id)
    await state.update_data(
        target_competition_id=str(comp.id),
        target_competition_slug=comp.slug,
        target_competition_title=comp.title,
        tracks_created=len(tracks),
        require_track=False,
    )
    await state.set_state(EditCompetitionFSM.choose_field)

    snapshot = await _competition_snapshot(comp, comp_svc, lz)
    choose_text = f"{lz.get('competitions.edit.choose_field')}\n\n{snapshot}"
    await cq.answer()
    await cq.message.edit_text(choose_text, reply_markup=_kb_edit_fields(lz))


@router.message(EditCompetitionFSM.waiting_target, F.text.regexp(r"[a-zA-Z0-9_]{3,}"))
async def edit_competition_by_slug(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    slug = _normalize_slug(message.text)
    comp_svc = CompetitionService()
    comp = await comp_svc.get_competition(slug)
    if comp is None:
        await message.answer(lz.get("competitions.edit.not_found"))
        return

    tracks = await comp_svc.list_tracks(comp.id)
    await state.update_data(
        target_competition_id=str(comp.id),
        target_competition_slug=comp.slug,
        target_competition_title=comp.title,
        tracks_created=len(tracks),
        require_track=False,
    )
    await state.set_state(EditCompetitionFSM.choose_field)
    snapshot = await _competition_snapshot(comp, comp_svc, lz)
    text = f"{lz.get('competitions.edit.user_selected')}\n\n{snapshot}"
    await message.answer(text, reply_markup=_kb_edit_fields(lz))


@router.callback_query(F.data.startswith("comp.field:"), EditCompetitionFSM.choose_field)
async def edit_comp_choose_field(cq: CallbackQuery, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    field = cq.data.split(":")[1]
    comp_svc = CompetitionService()
    data = await state.get_data()
    comp = await _ensure_competition(data.get("target_competition_id"), comp_svc)

    if field == "back":
        await state.set_state(EditCompetitionFSM.waiting_target)
        await cq.answer()
        await _open_competitions_page(cq, comp_svc, page=0, lz=lz)
        return

    if comp is None:
        await state.clear()
        await cq.answer(lz.get("competitions.edit.not_found"), show_alert=True)
        await cq.message.edit_text(lz.get("competitions.edit.not_found"), reply_markup=None)
        return

    await state.update_data(field=field)
    await state.set_state(EditCompetitionFSM.set_value)

    prompt_key = "competitions.edit.send_value"
    if field == "slug":
        prompt_key = "competitions.edit.send_slug"
    elif field in {"start_at", "end_at"}:
        prompt_key = "competitions.edit.send_datetime"
    await cq.answer()
    snapshot = await _competition_snapshot(comp, comp_svc, lz)
    prompt = lz.get(prompt_key, fmt=DATETIME_FMT)
    await cq.message.edit_text(f"{prompt}\n\n{snapshot}", reply_markup=None)


@router.message(EditCompetitionFSM.set_value, F.text)
async def edit_comp_apply(message: Message, current_user: UserRead, state: FSMContext) -> None:
    lz = await get_localizer_by_user(current_user)
    data = await state.get_data()
    field = data.get("field")
    if field is None:
        await message.answer(lz.get("competitions.edit.not_found"))
        return

    comp_svc = CompetitionService()
    comp = await _ensure_competition(data.get("target_competition_id"), comp_svc)
    if comp is None:
        await message.answer(lz.get("competitions.edit.not_found"))
        await state.clear()
        return

    payload_kwargs = {"id": comp.id}
    raw = message.text.strip()

    if field == "title":
        if not raw:
            await message.answer(lz.get("competitions.edit.send_value"))
            return
        payload_kwargs["title"] = raw
    elif field == "slug":
        slug = _normalize_slug(raw)
        if not _valid_slug(slug):
            await message.answer(lz.get("competitions.add.bad_slug"))
            return
        existing = await comp_svc.get_competition(slug)
        if existing is not None and existing.id != comp.id:
            await message.answer(lz.get("competitions.add.slug_exists"))
            return
        payload_kwargs["slug"] = slug
    elif field in {"start_at", "end_at"}:
        dt = _parse_datetime(raw)
        if dt is None:
            await message.answer(lz.get("competitions.add.bad_datetime", fmt=DATETIME_FMT))
            return
        if field == "start_at" and dt >= comp.end_at:
            await message.answer(lz.get("competitions.edit.start_after_end"))
            return
        if field == "end_at" and dt <= comp.start_at:
            await message.answer(lz.get("competitions.add.end_before_start"))
            return
        payload_kwargs[field] = dt
    else:
        await message.answer(lz.get("competitions.edit.unsupported"))
        return

    updated = await comp_svc.update_competition(CompetitionUpdate(**payload_kwargs))
    await state.set_state(EditCompetitionFSM.choose_field)
    await state.update_data(
        target_competition_slug=updated.slug,
        target_competition_title=updated.title,
    )

    snapshot = await _competition_snapshot(updated, comp_svc, lz)
    field_name = lz.get(f"competitions.fields.{field}")
    header = lz.get(
        "competitions.edit.updated",
        title=updated.title,
        field=field_name,
    )
    await message.answer(f"{header}\n\n{snapshot}", reply_markup=_kb_edit_fields(lz))