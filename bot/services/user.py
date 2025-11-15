# bot/services/user.py
from uuid import UUID
from typing import Self, ClassVar, Optional
from smart_solution.db.schemas.user import UserRead, UserCreate, UserUpdate
from smart_solution.db.enums import UiMode, UserRole
from smart_solution.db.database import DataBase
from smart_solution.bot.services.audit_log import instrument_service_class

class UserService:
    _instance: ClassVar[Optional["UserService"]] = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return

        self.database = DataBase()
        self.users: dict[int, UserRead] = dict()
        self._initialized = True

    async def list_users_page(self, page: int, page_size: int) -> tuple[list[UserRead], int]:
        """
        Deterministic paging: returns (items, total).
        Ordering is strictly by primary key 'id' (UUID) ascending.
        Also warms up tg_id cache for returned items.
        """
        limit = page_size
        offset = max(page, 0) * page_size

        items, total = await self.database.list_users(limit=limit, offset=offset)

        for u in items:
            if isinstance(u.tg_id, int):
                self.users[u.tg_id] = u
        return items, int(total)

    async def create_user(self, user: UserCreate) -> UserRead:
        new_user = await self.database.upsert_user(user)
        if new_user.tg_id is int:
            self.users[new_user.tg_id] = new_user
        return new_user

    async def update_user(self, user: UserUpdate) -> UserRead:
        new_user = await self.database.update_user(user)
        if type(new_user.tg_id) is int:
            self.users[new_user.tg_id] = new_user
        return new_user

    async def change_ui_mode(self, user: UserRead, ui_mode: UiMode) -> UserRead:
        updated_user = UserUpdate(id=user.id, ui_mode=ui_mode)
        return await self.update_user(updated_user)

    async def change_language(self, user: UserRead, lang_uuid: UUID) -> UserRead:
        updated_user = UserUpdate(id=user.id, preferred_language_id=lang_uuid)
        return await self.update_user(updated_user)

    async def change_role(self, user: UserRead, role: UserRole) -> UserRead:
        updated_user = UserUpdate(id=user.id, role=role)
        return await self.update_user(updated_user)

    async def get_user(self, uid: Optional[UUID] = None, tg_id: Optional[int | type(Ellipsis)] = ..., tg_username: Optional[str | type(Ellipsis)] = ..., autocreate: bool = False, autoupdate: bool = True) -> Optional[UserRead]:
        if tg_id in self.users:
            user = self.users.get(tg_id)
        else:
            user = await self.database.get_user(uid, tg_id if tg_id is not Ellipsis else None, tg_username if tg_username is not Ellipsis else None)
            if user is None:
                if autocreate and (type(tg_id) is int):
                    new_user = UserCreate(tg_id=tg_id, tg_username=tg_username if tg_username is not Ellipsis else None)
                    user = await self.database.create_user(new_user)

            if (user is not None) and (type(tg_id) is int):
                self.users[tg_id] = user

        if (user is not None) and autoupdate:
            upd = False
            updated_user = UserUpdate(id=user.id)
            if ((tg_username is not Ellipsis) and (user.tg_username != tg_username)):
                updated_user.tg_username = tg_username
                upd = True
            if ((tg_id is not Ellipsis) and (user.tg_id != tg_id)):
                updated_user.tg_id = tg_id
                upd = True

            if upd:
                user = await self.update_user(updated_user)

        return user


instrument_service_class(
    UserService,
    prefix="services.user",
    actor_fields=("user", "actor"),
    exclude={"get_user"},
)
