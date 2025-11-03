# bot/routers/submissions_user.py
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Document, Message
from aiogram.exceptions import TelegramBadRequest

from smart_solution.db.enums import UiMode
from smart_solution.db.schemas.submission import SubmissionCreate
from smart_solution.bot.services.auto_judge import auto_judge
from smart_solution.db.schemas.user import UserRead, UserUpdate
from smart_solution.bot.filters.action_like import ActionLike
from smart_solution.bot.keyboards.user_keyboard_factory import UserKeyboardFactory
from smart_solution.bot.routers.utils import get_localizer_by_user
from smart_solution.bot.services.competition import CompetitionService
from smart_solution.bot.services.page import PageService
from smart_solution.bot.services.submission import SubmissionService
from smart_solution.bot.services.team import TeamService
from smart_solution.bot.services.user import UserService

router = Router(name="contestant_submission")
SUBMISSIONS_ROOT = Path(__file__).resolve().parents[2] / "data" / "submissions"
logger = logging.getLogger(__name__)


async def _get_active_team(user: UserRead, message: Message) -> Optional[uuid.UUID]:
    if user.active_team_id:
        return user.active_team_id

    team_svc = TeamService()
    teams = await team_svc.get_user_teams(user)
    if len(teams) == 1:
        await UserService().update_user(UserUpdate(id=user.id, active_team_id=teams[0].id))
        user.active_team_id = teams[0].id
        return teams[0].id

    lz = await get_localizer_by_user(user)
    await message.answer(lz.get("team_user.errors.no_team"))
    return None


async def _load_instruction(team_id: uuid.UUID, user: UserRead) -> Optional[str]:
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

    slug = f"instruction_{track.slug}"
    page_service = PageService()
    page = None
    if user.preferred_language_id:
        page = await page_service.get_page(competition.id, slug, user.preferred_language_id)
    if page is None:
        page = await page_service.get_page(competition.id, slug)
    if page is None:
        return None
    path = page_service.get_content_path(page.file_basename)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


async def _return_home(message: Message, user: UserRead, text: str, state: Optional[FSMContext] = None) -> UserRead:
    if state is not None:
        await state.clear()
    user_svc = UserService()
    user = await user_svc.change_ui_mode(user, UiMode.HOME)
    keyboard = await UserKeyboardFactory().build_for_user(user)
    await message.answer(text, reply_markup=keyboard)
    return user


@router.message(ActionLike("buttons.submit:home:contestant"))
async def start_submission(message: Message, current_user: UserRead, state: FSMContext) -> None:
    await state.clear()
    team_id = await _get_active_team(current_user, message)
    if team_id is None:
        return

    team_svc = TeamService()
    team = await team_svc.get_team(team_id)
    comp_svc = CompetitionService()
    if team is None or (team.track_id and await team_svc.is_competition_finished(team)):
        lz = await get_localizer_by_user(current_user)
        await message.answer(lz.get("team_user.submit.competition_finished"))
        return

    if not await team_svc.can_team_submit(current_user):
        lz = await get_localizer_by_user(current_user)
        await message.answer(lz.get("team_user.submit.limit_reached"))
        return

    track = await comp_svc.get_track_by_id(team.track_id) if team.track_id else None
    submission_service = SubmissionService()
    submissions_used = await submission_service.count_submissions(team)
    track_limit = track.max_submissions_total if track else None

    user_svc = UserService()
    current_user = await user_svc.change_ui_mode(current_user, UiMode.SUBMIT)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)

    lz = await get_localizer_by_user(current_user)

    parts = [lz.get("team_user.submit.entered")]
    if track_limit is not None:
        parts.append(
            lz.get(
                "team_user.submit.limit_info",
                used=str(submissions_used),
                limit=str(track_limit),
            )
        )
    else:
        parts.append(lz.get("team_user.submit.used", used=str(submissions_used)))
    parts.append(lz.get("team_user.submit.prompt"))

    await message.answer("\n\n".join(parts), reply_markup=keyboard)


@router.message(F.document)
async def receive_submission_document(message: Message, current_user: UserRead, state: FSMContext, bot: Bot) -> None:
    if current_user.ui_mode != UiMode.SUBMIT:
        return

    team_id_raw = current_user.active_team_id
    if team_id_raw is None:
        resolved = await _get_active_team(current_user, message)
        if resolved is None:
            return
        team_id_raw = resolved
        current_user.active_team_id = team_id_raw

    team_id = team_id_raw if isinstance(team_id_raw, uuid.UUID) else uuid.UUID(str(team_id_raw))

    lz = await get_localizer_by_user(current_user)
    doc: Document = message.document
    filename = doc.file_name or ""
    if not filename.lower().endswith(".zip"):
        await message.answer(lz.get("team_user.submit.not_zip"))
        return

    team_svc = TeamService()
    team = await team_svc.get_team(team_id)
    if team is None:
        await _return_home(message, current_user, lz.get("team_user.submit.error"), state)
        return

    if await team_svc.is_competition_finished(team):
        await _return_home(message, current_user, lz.get("team_user.submit.competition_finished"), state)
        return

    if not await team_svc.can_team_submit(current_user):
        await _return_home(message, current_user, lz.get("team_user.submit.limit_reached"), state)
        return

    membership = await team_svc.get_membership(current_user.id, team.id)
    if membership is None:
        await _return_home(message, current_user, lz.get("team_user.submit.no_membership"), state)
        return

    submission_service = SubmissionService()
    existing_count = await submission_service.count_submissions(team)
    sequence_number = existing_count + 1
    raw_slug = team.slug or str(team.id)
    safe_slug = raw_slug.strip().replace(" ", "_")
    title = f"{safe_slug} #{sequence_number}"

    filename = f"{safe_slug}_{sequence_number}.zip"
    team_dir = SUBMISSIONS_ROOT / str(team_id)
    team_dir.mkdir(parents=True, exist_ok=True)
    dest_path = team_dir / filename
    progress_message: Optional[Message] = None

    try:
        file_info = await bot.get_file(doc.file_id)
        total_size = file_info.file_size or doc.file_size or 0
        if total_size:
            progress_message = await message.answer(
                lz.get("team_user.submit.downloading", progress="0%")
            )
        else:
            progress_message = await message.answer(
                lz.get("team_user.submit.downloading_bytes", downloaded=_format_size(0))
            )

        await _download_with_progress(
            bot=bot,
            file_path=file_info.file_path,
            destination=dest_path,
            progress_message=progress_message,
            lz=lz,
            total_size=total_size,
        )
    except Exception:
        fail_text = lz.get("team_user.submit.download_failed")
        await _safe_edit_progress(progress_message, fail_text)
        dest_path.unlink(missing_ok=True)
        if progress_message is None:
            await message.answer(fail_text)
        return

    relative_path = dest_path.relative_to(SUBMISSIONS_ROOT.parent)
    try:
        submission = await submission_service.create_submission(
            SubmissionCreate(
                team_user_id=membership.id,
                title=title,
                file_path=str(relative_path),
            )
        )
    except Exception:
        await message.answer(lz.get("team_user.submit.error"))
        return

    current_user = await _return_home(
        message,
        current_user,
        lz.get("team_user.submit.saved", submission_id=str(submission.id), title=title),
        state,
    )

    auto_result = auto_judge.pop_result(submission.id)
    if auto_result is not None:
        if auto_result.success:
            status_text = (
                lz.get(f"submissions.status.{auto_result.status.value}")
                if auto_result.status
                else lz.get("team_user.submit.auto_unknown_status")
            )
            value_text = _format_value(auto_result.value)
            reply = auto_result.message or lz.get(
                "team_user.submit.auto_success",
                status=status_text,
                value=value_text,
            )
            await message.answer(reply)
        else:
            reply = auto_result.message or lz.get("team_user.submit.auto_failed")
            await message.answer(reply)


@router.message(ActionLike("buttons.instructions:submit:contestant"))
async def show_submission_instructions(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if current_user.ui_mode != UiMode.SUBMIT:
        return

    lz = await get_localizer_by_user(current_user)
    team_id_raw = current_user.active_team_id
    if team_id_raw is None:
        resolved = await _get_active_team(current_user, message)
        if resolved is None:
            return
        team_id_raw = resolved
        current_user.active_team_id = team_id_raw

    team_id = team_id_raw if isinstance(team_id_raw, uuid.UUID) else uuid.UUID(str(team_id_raw))
    instruction = await _load_instruction(team_id, current_user)

    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    if instruction:
        header = lz.get("team_user.submit.instructions_title")
        await message.answer(f"{header}\n\n{instruction}", disable_web_page_preview=True, reply_markup=keyboard)
    else:
        await message.answer(lz.get("team_user.submit.default"), reply_markup=keyboard)


@router.message(ActionLike("buttons.back:submit:contestant"))
async def submission_back_home(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if current_user.ui_mode != UiMode.SUBMIT:
        return
    lz = await get_localizer_by_user(current_user)
    await _return_home(message, current_user, lz.get("team_user.submit.cancelled"), state)


def _format_size(num_bytes: int) -> str:
    value = float(max(num_bytes, 0))
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _format_value(value: Optional[float]) -> str:
    if value is None:
        return "—"
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


async def _safe_edit_progress(message: Optional[Message], text: str) -> None:
    if message is None:
        return
    try:
        await message.edit_text(text)
    except TelegramBadRequest:
        pass


async def _download_with_progress(
    bot: Bot,
    file_path: str,
    destination: Path,
    progress_message: Optional[Message],
    lz,
    total_size: int,
    chunk_size: int = 65536,
) -> None:
    local_src = Path(file_path)
    url = bot.session.api.file_url(bot.token, file_path)
    if file_path.startswith("/") and f"/{bot.token}/" in file_path:
        prefix = bot.session.api.file_url(bot.token, "")
        relative = file_path.split(f"/{bot.token}/", 1)[1].lstrip("/")
        url = f"{prefix}{relative}"
    logger.info("Starting download: path=%s url=%s -> %s", file_path, url, destination)
    downloaded = 0
    last_percent = -5
    last_update = time.monotonic()

    async def _bump_progress(amount: int) -> None:
        nonlocal downloaded, last_percent, last_update
        downloaded += amount
        if total_size:
            percent = min(100, int(downloaded * 100 / total_size))
            if percent >= last_percent + 5 or percent == 100:
                await _safe_edit_progress(
                    progress_message,
                    lz.get("team_user.submit.downloading", progress=f"{percent}%"),
                )
                last_percent = percent
        else:
            now = time.monotonic()
            if now - last_update >= 0.7:
                await _safe_edit_progress(
                    progress_message,
                    lz.get(
                        "team_user.submit.downloading_bytes",
                        downloaded=_format_size(downloaded),
                    ),
                )
                last_update = now

    try:
        if local_src.is_absolute() and local_src.exists():
            logger.info("Copying submission from local filesystem %s", local_src)
            if not total_size:
                try:
                    total_size = local_src.stat().st_size
                except OSError:
                    total_size = 0
            with local_src.open("rb") as src, destination.open("wb") as dst:
                while True:
                    chunk = src.read(chunk_size)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await _bump_progress(len(chunk))
        else:
            with destination.open("wb") as raw:
                async for chunk in bot.session.stream_content(url, chunk_size=chunk_size):
                    raw.write(chunk)
                    await _bump_progress(len(chunk))
    except Exception:
        logger.exception("Download failed from %s", url)
        raise

    await _safe_edit_progress(
        progress_message,
        lz.get(
            "team_user.submit.download_complete",
            size=_format_size(downloaded),
        ),
    )
