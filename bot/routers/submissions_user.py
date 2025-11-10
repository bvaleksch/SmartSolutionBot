# bot/routers/submissions_user.py
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Document, Message
from aiogram.exceptions import TelegramBadRequest

from smart_solution.config import Settings
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
FILE_PART_LIMIT_BYTES = max(1_048_576, Settings().submission_file_part_max_bytes)
MULTIPART_SAMPLE_FILENAME = "solution.zip"
logger = logging.getLogger(__name__)
ZIP_PART_PATTERN = re.compile(r".+\.zip\.part\d+$", re.IGNORECASE)


@dataclass
class SubmissionPlan:
    team_id: uuid.UUID
    membership_id: uuid.UUID
    title: str
    safe_slug: str
    sequence_number: int
    dest_path: Path
    relative_path: str


class SubmissionMultipartFSM(StatesGroup):
    waiting_total = State()
    collecting = State()


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

    lz = await get_localizer_by_user(current_user)
    state_name = await state.get_state()
    if state_name == SubmissionMultipartFSM.collecting.state:
        await _handle_multipart_part(message, current_user, state, bot, lz)
        return
    if state_name == SubmissionMultipartFSM.waiting_total.state:
        await message.answer(lz.get("team_user.submit.multipart_need_total"))
        return

    doc: Document = message.document
    filename = doc.file_name or ""
    if _is_zip_part(filename):
        await message.answer(lz.get("team_user.submit.multipart_not_started"))
        return
    if not _is_zip_file(filename):
        await message.answer(lz.get("team_user.submit.not_zip"))
        return

    plan, current_user = await _prepare_submission_plan(message, current_user, state, lz)
    if plan is None:
        return

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
            destination=plan.dest_path,
            progress_message=progress_message,
            lz=lz,
            total_size=total_size,
        )
    except Exception:
        fail_text = lz.get("team_user.submit.download_failed")
        await _safe_edit_progress(progress_message, fail_text)
        plan.dest_path.unlink(missing_ok=True)
        if progress_message is None:
            await message.answer(fail_text)
        return

    submission_service = SubmissionService()
    try:
        submission = await submission_service.create_submission(
            SubmissionCreate(
                team_user_id=plan.membership_id,
                title=plan.title,
                file_path=plan.relative_path,
            )
        )
    except Exception:
        await message.answer(lz.get("team_user.submit.error"))
        return

    current_user = await _return_home(
        message,
        current_user,
        lz.get("team_user.submit.saved", submission_id=str(submission.id), title=plan.title),
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


@router.message(ActionLike("buttons.send_parts:submit:contestant"))
async def show_multipart_hint(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if current_user.ui_mode != UiMode.SUBMIT:
        return

    lz = await get_localizer_by_user(current_user)
    keyboard = await UserKeyboardFactory().build_for_user(current_user)
    await message.answer(
        _build_multipart_instructions(lz),
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await state.set_state(SubmissionMultipartFSM.waiting_total)
    await state.update_data(
        multipart_expected_parts=None,
        multipart_received_parts=0,
        multipart_parts=None,
        multipart_plan=None,
        multipart_tmp_dir=None,
        multipart_first_part=None,
    )
    await message.answer(lz.get("team_user.submit.multipart_ask_total"))


@router.message(SubmissionMultipartFSM.waiting_total, F.text)
async def multipart_set_total(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if current_user.ui_mode != UiMode.SUBMIT:
        return

    lz = await get_localizer_by_user(current_user)
    text = (message.text or "").strip()
    if text.lower() in {"cancel", "stop"}:
        await state.clear()
        await message.answer(lz.get("team_user.submit.multipart_cancelled"))
        return

    try:
        total = int(text)
    except ValueError:
        await message.answer(lz.get("team_user.submit.multipart_invalid_total"))
        return

    if total < 2 or total > 20:
        await message.answer(lz.get("team_user.submit.multipart_invalid_total"))
        return

    await state.update_data(
        multipart_expected_parts=total,
        multipart_received_parts=0,
        multipart_parts=[],
        multipart_plan=None,
        multipart_tmp_dir=None,
        multipart_first_part=None,
    )
    await state.set_state(SubmissionMultipartFSM.collecting)
    await message.answer(
        lz.get(
            "team_user.submit.multipart_ready",
            total=str(total),
            next="1",
        )
    )


async def _handle_multipart_part(
    message: Message,
    current_user: UserRead,
    state: FSMContext,
    bot: Bot,
    lz,
) -> None:
    data = await state.get_data()
    expected = data.get("multipart_expected_parts")
    if not expected:
        await state.clear()
        await message.answer(lz.get("team_user.submit.multipart_missing_setup"))
        return

    doc: Document = message.document
    filename = doc.file_name or ""
    if not _is_zip_part(filename):
        await message.answer(lz.get("team_user.submit.not_zip"))
        return

    part_number = _extract_part_number(filename)
    if part_number is None:
        await message.answer(lz.get("team_user.submit.not_zip"))
        return

    base_number_raw = data.get("multipart_first_part")
    try:
        base_number = int(base_number_raw) if base_number_raw is not None else None
    except (TypeError, ValueError):
        base_number = None
    received = int(data.get("multipart_received_parts") or 0)
    if base_number is None:
        if part_number not in {0, 1}:
            await message.answer(
                lz.get(
                    "team_user.submit.multipart_wrong_order",
                    expected="0 or 1",
                    got=str(part_number),
                )
            )
            return
        base_number = part_number
        await state.update_data(multipart_first_part=base_number)

    expected_part_number = base_number + received
    if part_number != expected_part_number:
        await message.answer(
            lz.get(
                "team_user.submit.multipart_wrong_order",
                expected=str(expected_part_number),
                got=str(part_number),
            )
        )
        return

    plan_dict = data.get("multipart_plan")
    if plan_dict is None:
        plan, current_user = await _prepare_submission_plan(message, current_user, state, lz)
        if plan is None:
            await state.clear()
            return
        plan_dict = _plan_to_dict(plan)
        await state.update_data(multipart_plan=plan_dict)
    else:
        plan = _plan_from_dict(plan_dict)
        if plan is None:
            await state.clear()
            await message.answer(lz.get("team_user.submit.multipart_missing_setup"))
            return

    tmp_dir_str = data.get("multipart_tmp_dir")
    if tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
    else:
        tmp_dir = SUBMISSIONS_ROOT / "tmp" / plan.safe_slug / f"{plan.sequence_number}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        await state.update_data(multipart_tmp_dir=str(tmp_dir))

    part_path = tmp_dir / (filename or f"part_{part_number:03d}")
    part_path.parent.mkdir(parents=True, exist_ok=True)

    display_part_index = received + 1
    tracker = OverallPartsProgress(
        total_parts=int(expected),
        completed_parts=received,
        current_part=display_part_index,
    )

    progress_message: Optional[Message] = None
    try:
        file_info = await bot.get_file(doc.file_id)
        total_size = file_info.file_size or doc.file_size or 0
        if total_size:
            progress_message = await message.answer(
                lz.get(
                    "team_user.submit.downloading_overall",
                    progress="0%",
                    part=str(display_part_index),
                    total=str(expected),
                )
            )
        else:
            progress_message = await message.answer(
                lz.get(
                    "team_user.submit.downloading_overall_bytes",
                    downloaded=_format_size(0),
                    part=str(display_part_index),
                    total=str(expected),
                )
            )

        await _download_with_progress(
            bot=bot,
            file_path=file_info.file_path,
            destination=part_path,
            progress_message=progress_message,
            lz=lz,
            total_size=total_size,
            overall_tracker=tracker,
        )
    except Exception:
        fail_text = lz.get("team_user.submit.download_failed")
        await _safe_edit_progress(progress_message, fail_text)
        part_path.unlink(missing_ok=True)
        if progress_message is None:
            await message.answer(fail_text)
        return

    parts = list(data.get("multipart_parts") or [])
    parts.append(str(part_path))
    received += 1
    await state.update_data(
        multipart_parts=parts,
        multipart_received_parts=received,
    )

    await message.answer(
        lz.get(
            "team_user.submit.multipart_part_saved",
            current=str(received),
            total=str(expected),
        )
    )

    if received < expected:
        await message.answer(
            lz.get(
                "team_user.submit.multipart_next",
                next=str(received + 1),
                total=str(expected),
            )
        )
        return

    await message.answer(lz.get("team_user.submit.multipart_assembling"))
    await _assemble_and_submit_parts(message, current_user, state, plan, parts, lz)


@router.message(ActionLike("buttons.back:submit:contestant"))
async def submission_back_home(message: Message, current_user: UserRead, state: FSMContext) -> None:
    if current_user.ui_mode != UiMode.SUBMIT:
        return
    lz = await get_localizer_by_user(current_user)
    await _return_home(message, current_user, lz.get("team_user.submit.cancelled"), state)


def _build_multipart_instructions(lz) -> str:
    limit_bytes = max(1, FILE_PART_LIMIT_BYTES)
    limit_label = _format_size(limit_bytes)
    limit_mb = max(1, int(round(limit_bytes / (1024 * 1024))))
    sample = MULTIPART_SAMPLE_FILENAME
    parts = [
        lz.get("team_user.submit.multipart_title", limit=limit_label),
        lz.get("team_user.submit.multipart_unix", sample=sample, limit_mb=str(limit_mb)),
        lz.get("team_user.submit.multipart_windows", sample=sample, limit_mb=str(limit_mb)),
        lz.get("team_user.submit.multipart_upload", sample=sample),
        lz.get("team_user.submit.multipart_concat", sample=sample),
    ]
    return "\n\n".join(parts)


def _plan_to_dict(plan: SubmissionPlan) -> dict:
    return {
        "team_id": str(plan.team_id),
        "membership_id": str(plan.membership_id),
        "title": plan.title,
        "safe_slug": plan.safe_slug,
        "sequence_number": plan.sequence_number,
        "dest_path": str(plan.dest_path),
        "relative_path": plan.relative_path,
    }


def _plan_from_dict(data: Optional[dict]) -> Optional[SubmissionPlan]:
    if not data:
        return None
    try:
        return SubmissionPlan(
            team_id=uuid.UUID(data["team_id"]),
            membership_id=uuid.UUID(data["membership_id"]),
            title=data["title"],
            safe_slug=data["safe_slug"],
            sequence_number=int(data["sequence_number"]),
            dest_path=Path(data["dest_path"]),
            relative_path=data["relative_path"],
        )
    except (KeyError, ValueError):
        return None


@dataclass
class OverallPartsProgress:
    total_parts: int
    completed_parts: int
    current_part: int

    def percent(self, current_downloaded: int, current_total: int | None) -> Optional[float]:
        if not current_total:
            return None
        if self.total_parts <= 0:
            return None
        fraction = min(1.0, max(0.0, current_downloaded / max(1, current_total)))
        overall = (self.completed_parts + fraction) / self.total_parts
        return max(0.0, min(1.0, overall)) * 100.0


def _extract_part_number(filename: str) -> Optional[int]:
    if not filename:
        return None
    match = re.search(r"\.part(\d+)$", filename, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _is_zip_file(filename: str) -> bool:
    return (filename or "").lower().endswith(".zip")


def _is_zip_part(filename: str) -> bool:
    if not filename:
        return False
    return bool(ZIP_PART_PATTERN.match(filename))


def _is_allowed_submission_file(filename: str) -> bool:
    return _is_zip_file(filename) or _is_zip_part(filename)


async def _prepare_submission_plan(
    message: Message,
    current_user: UserRead,
    state: FSMContext,
    lz,
) -> tuple[Optional[SubmissionPlan], UserRead]:
    team_id_raw = current_user.active_team_id
    if team_id_raw is None:
        resolved = await _get_active_team(current_user, message)
        if resolved is None:
            return None, current_user
        team_id_raw = resolved
        current_user.active_team_id = team_id_raw

    team_id = team_id_raw if isinstance(team_id_raw, uuid.UUID) else uuid.UUID(str(team_id_raw))

    team_svc = TeamService()
    team = await team_svc.get_team(team_id)
    if team is None:
        updated = await _return_home(message, current_user, lz.get("team_user.submit.error"), state)
        return None, updated

    if await team_svc.is_competition_finished(team):
        updated = await _return_home(message, current_user, lz.get("team_user.submit.competition_finished"), state)
        return None, updated

    if not await team_svc.can_team_submit(current_user):
        updated = await _return_home(message, current_user, lz.get("team_user.submit.limit_reached"), state)
        return None, updated

    membership = await team_svc.get_membership(current_user.id, team.id)
    if membership is None:
        updated = await _return_home(message, current_user, lz.get("team_user.submit.no_membership"), state)
        return None, updated

    submission_service = SubmissionService()
    existing_count = await submission_service.count_submissions(team)
    sequence_number = existing_count + 1
    raw_slug = team.slug or str(team.id)
    safe_slug = raw_slug.strip().replace(" ", "_") or str(team.id)
    title = f"{safe_slug} #{sequence_number}"

    filename = f"{safe_slug}_{sequence_number}.zip"
    team_dir = SUBMISSIONS_ROOT / str(team_id)
    team_dir.mkdir(parents=True, exist_ok=True)
    dest_path = team_dir / filename
    relative_path = dest_path.relative_to(SUBMISSIONS_ROOT.parent)

    plan = SubmissionPlan(
        team_id=team_id,
        membership_id=membership.id,
        title=title,
        safe_slug=safe_slug,
        sequence_number=sequence_number,
        dest_path=dest_path,
        relative_path=str(relative_path),
    )
    return plan, current_user


async def _assemble_and_submit_parts(
    message: Message,
    current_user: UserRead,
    state: FSMContext,
    plan: SubmissionPlan,
    parts: list[str],
    lz,
) -> None:
    plan.dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with plan.dest_path.open("wb") as dest:
            for part_file in parts:
                part_path = Path(part_file)
                with part_path.open("rb") as src:
                    shutil.copyfileobj(src, dest)
    except Exception:
        plan.dest_path.unlink(missing_ok=True)
        await message.answer(lz.get("team_user.submit.multipart_assemble_failed"))
        return

    submission_service = SubmissionService()
    try:
        submission = await submission_service.create_submission(
            SubmissionCreate(
                team_user_id=plan.membership_id,
                title=plan.title,
                file_path=plan.relative_path,
            )
        )
    except Exception:
        plan.dest_path.unlink(missing_ok=True)
        await message.answer(lz.get("team_user.submit.error"))
        return
    finally:
        for part_file in parts:
            Path(part_file).unlink(missing_ok=True)
        data = await state.get_data()
        tmp_dir = data.get("multipart_tmp_dir")
        if tmp_dir:
            _cleanup_tmp_dir(Path(tmp_dir))

    await state.clear()
    current_user = await _return_home(
        message,
        current_user,
        lz.get("team_user.submit.saved", submission_id=str(submission.id), title=plan.title),
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


def _cleanup_tmp_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        return
    parent = path.parent
    base_tmp = SUBMISSIONS_ROOT / "tmp"
    try:
        if parent.is_dir() and parent != base_tmp and not any(parent.iterdir()):
            parent.rmdir()
        if base_tmp.is_dir() and not any(base_tmp.iterdir()):
            base_tmp.rmdir()
    except OSError:
        pass


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
    overall_tracker: OverallPartsProgress | None = None,
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
        if overall_tracker:
            if total_size:
                percent_value = overall_tracker.percent(downloaded, total_size)
                if percent_value is not None:
                    percent = int(percent_value)
                    if percent >= last_percent + 5 or percent >= 100 or last_percent < 0:
                        await _safe_edit_progress(
                            progress_message,
                            lz.get(
                                "team_user.submit.downloading_overall",
                                progress=f"{percent}%",
                                part=str(overall_tracker.current_part),
                                total=str(overall_tracker.total_parts),
                            ),
                        )
                        last_percent = percent
            else:
                now = time.monotonic()
                if now - last_update >= 0.7:
                    await _safe_edit_progress(
                        progress_message,
                        lz.get(
                            "team_user.submit.downloading_overall_bytes",
                            downloaded=_format_size(downloaded),
                            part=str(overall_tracker.current_part),
                            total=str(overall_tracker.total_parts),
                        ),
                    )
                    last_update = now
        else:
            if total_size:
                percent = min(100, int(downloaded * 100 / max(1, total_size)))
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
