# bot/keyboards/user_keyboard_factory.py
from uuid import UUID
from typing import Self, ClassVar, Optional, Dict, Tuple, List
from aiogram.types.keyboard_button import KeyboardButton
from aiogram.types import ReplyKeyboardMarkup
from smart_solution.db.schemas.user import UserRead
from smart_solution.i18n import Localizer
from smart_solution.db.enums import UserRole, UiMode
from smart_solution.db.schemas.language import LanguageRead
from smart_solution.bot.services.language import LanguageService
from smart_solution.bot.services.action_registry import ActionRegistry
from smart_solution.bot.keyboards.keyboard_context import KeyboardContext


class UserKeyboardFactory:
    _instance: ClassVar[Optional["UserKeyboardFactory"]] = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return

        self._localizers: Dict[UUID, Localizer] = dict()
        self._action_registry = ActionRegistry()
        self._user_keyboard: Dict[UUID, int] = dict() 

        self._initialized = True

    async def is_stale(self, user: UserRead) -> bool:
        if user.id not in self._user_keyboard:
            return True
        
        cntx = KeyboardContext(user)
        
        return (self._user_keyboard.get(user.id) == await hash(cntx))

    async def get_localizer(self, lang_id: UUID) -> Localizer:
        if lang_id not in self._localizers:
            lng_svc = LanguageService()
            lang = await lng_svc.safe_autoget(lang_id)
            localizer = Localizer(lang.name)

            if lang.id == lang_id:
                self._localizers[lang_id] = localizer
            else:
                if lang.id not in self._localizers:
                    self._localizers[lang.id] = localizer
                return self._localizers.get(lang.id)

        return self._localizers.get(lang_id)

    async def build_for_user(self, user: UserRead) -> ReplyKeyboardMarkup:
        """
        Automatically choose the right keyboard based on user role
        """
        localizer = await self.get_localizer(user.preferred_language_id)
        buttons: List[List[KeyboardButton]] = list()
        if user.role == UserRole.UNREGISTERED:
            self.for_unregistered(buttons, user.ui_mode, localizer)
        elif user.role == UserRole.ADMIN:
            await self.for_admin(buttons, user, localizer)
        else:
            await self.for_contestant(buttons, user, localizer)

        self._user_keyboard[user.id] = await KeyboardContext(user).myhash()

        keyboard = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=False)

        return keyboard

    def _build(self, buttons: List[List[KeyboardButton]], btns: List[List[str]], localizer: Localizer, ui_mode: UiMode, role: UserRole):
        for row in btns:
            buttons.append([KeyboardButton(text=localizer.get(text)) for text in row])
            for text in row:
                self._action_registry.register(text=localizer.get(text), ui_mode=ui_mode, role=str(role), action=f"{text}:{ui_mode}:{role}")

    @staticmethod
    def _change_language() -> List[List[str]]:
        rows = [["buttons.back"]]
        return rows

    @staticmethod
    def _base() -> List[List[str]]:
        rows = [["buttons.help", "buttons.profile"],
                ["buttons.change_language"]]
        return rows

    def for_unregistered(self, buttons: List[List[KeyboardButton]], ui_mode: UiMode, localizer: Localizer) -> None:
        if ui_mode == UiMode.HOME:
            btns = self._base()
        if ui_mode == UiMode.CHANGE_LANGUAGE:
            btns = self._change_language()

        self._build(buttons, btns, localizer, ui_mode, UserRole.UNREGISTERED)

    async def for_contestant(self, buttons: List[List[KeyboardButton]], user: UserRead, localizer: Localizer) -> None:
        cntx = KeyboardContext(user)
        await cntx.initialize()
        if user.ui_mode == UiMode.TEAM:
            btns: List[List[str]] = [["buttons.about", "buttons.rule"]]
            row = ["buttons.back"]
            if cntx.can_switch_team:
                row.insert(0, "buttons.change_team")
            btns.append(row)
        elif user.ui_mode == UiMode.SUBMIT:
            btns = [["buttons.instructions"],
                    ["buttons.back"]]
        elif user.ui_mode == UiMode.CHANGE_LANGUAGE:
            btns = self._change_language()
        else:
            row1 = []
            if cntx.can_switch_team or cntx.has_selected_team:
                row1.append("buttons.team")
            if cntx.has_selected_team:
                row1.append("buttons.leaderboard")
            if cntx.can_submit:
                row1.append("buttons.submit")
            btns = [row1] if row1 else []
            btns.append(["buttons.profile", "buttons.help"])
            btns.append(["buttons.change_language"])

        self._build(buttons, btns, localizer, user.ui_mode, UserRole.CONTESTANT)

    async def for_admin(self, buttons: List[List[KeyboardButton]], user: UserRead, localizer: Localizer) -> None:
        btns = []
        if user.ui_mode == UiMode.HOME:
            btns = [["buttons.user", "buttons.team"],
                    ["buttons.competition", "buttons.submission"],
                    ["buttons.help", "buttons.profile"],
                    ["buttons.change_language"]]
        elif user.ui_mode == UiMode.COMPETITION:
            btns = [["buttons.add_competition", "buttons.edit_competition"],
                    ["buttons.back"]]
        elif user.ui_mode == UiMode.EDIT_COMPETITION:
            btns = [["buttons.add_track"],
                    ["buttons.cancel"]]
        elif user.ui_mode == UiMode.TEAM:
            btns = [["buttons.add_team", "buttons.edit_team"],
                    ["buttons.back"]]
        elif user.ui_mode == UiMode.NEW_TEAM:
            btns = [["buttons.cancel"]]
        elif user.ui_mode == UiMode.EDIT_TEAM:
            btns = [["buttons.back"],
                    ["buttons.cancel"]]
        elif user.ui_mode == UiMode.NEW_COMPETITION:
            btns = [["buttons.add_track"],
                    ["buttons.cancel"]]
        elif user.ui_mode == UiMode.USER:
            btns = [["buttons.add_user", "buttons.edit_user"],
                    ["buttons.include_user_team"],
                    ["buttons.back"]]
        elif user.ui_mode == UiMode.NEW_USER:
            btns = [["buttons.skip"],
                    ["buttons.cancel"]]
        elif user.ui_mode == UiMode.EDIT_USER:
            btns = [["buttons.back"],
                    ["buttons.cancel"]]
        elif user.ui_mode == UiMode.SUBMISSION:
            btns = [["buttons.view_submission", "buttons.rate_submission"],
                    ["buttons.rerate_submission", "buttons.back"]]
        elif user.ui_mode == UiMode.CHANGE_LANGUAGE:
            btns = self._change_language()
        else:
            btns = [["buttons.user", "buttons.team"],
                    ["buttons.profile", "buttons.submission"],
                    ["buttons.help", "buttons.change_language"]]

        self._build(buttons, btns, localizer, user.ui_mode, UserRole.ADMIN)
