# db/database.py
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, ClassVar, Self, Any, List, Tuple

from sqlalchemy import select, func, text, and_
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.exc import IntegrityError

from smart_solution.db.models.track import Track
from smart_solution.db.models.competition import Competition
from smart_solution.db.models._base import Base
from smart_solution.db.models.user import User
from smart_solution.db.models.team import Team
from smart_solution.db.models.team_user import TeamUser
from smart_solution.db.models.page import Page
from smart_solution.db.schemas.page import PageRead, PageCreate, PageUpdate
from smart_solution.db.schemas.track import TrackRead, TrackCreate, TrackUpdate, TrackLeaderboardRow
from smart_solution.db.schemas.competition import CompetitionRead, CompetitionCreate, CompetitionUpdate
from smart_solution.db.schemas.team import TeamCreate, TeamUpdate, TeamRead
from smart_solution.db.schemas.team_user import (
    TeamUserCreate, TeamUserUpdate, TeamUserRead,
)
from smart_solution.db.enums import UserRole, ContestantRole, SubmissionStatus, SortDirection
from smart_solution.db.schemas.user import UserCreate, UserRead, UserUpdate
from smart_solution.db.models.language import Language
from smart_solution.db.schemas.language import LanguageRead
from smart_solution.config import Settings
from smart_solution.db.models.submission import Submission
from smart_solution.db.schemas.submission import SubmissionRead, SubmissionCreate, SubmissionUpdate
from smart_solution.db.models.audit_log import AuditLog
from smart_solution.db.schemas.audit_log import AuditLogCreate, AuditLogRead
from smart_solution.utils.sentinels import MISSING


class DataBase():
    """
    Async SQLAlchemy database singleton.
    Usage:
        db = DataBase()  # same instance everywhere
        async with db.session() as s:
            ...
    """
    _instance: ClassVar[Optional["DataBase"]] = None

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)

        return cls._instance

    def __init__(self, echo: bool = False) -> None:
        if getattr(self, "_initialized", False):
            return

        url = Settings().database_url
        self._engine: AsyncEngine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

        self._schema_ready = False
        self._initialized = True

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """
        Provides an AsyncSession with safe commit/rollback semantics.
        """
        session: AsyncSession = self._sessionmaker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    # --- schema management helpers (optional) ---

    async def create_all(self) -> None:
        """
        Create tables based on Base metadata. Use only in dev/tests; prefer Alembic in prod.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                text(
                    "ALTER TABLE track "
                    "ADD COLUMN IF NOT EXISTS slug VARCHAR(128)"
                )
            )
            await conn.execute(
                text(
                    "UPDATE track "
                    "SET slug = lower(regexp_replace(coalesce(title, ''), '[^a-z0-9_]+', '_', 'gi')) "
                    "WHERE slug IS NULL OR slug = ''"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE page "
                    "ADD COLUMN IF NOT EXISTS language_id UUID"
                )
            )
            await conn.execute(
                text(
                    "DO $$ BEGIN "
                    "ALTER TABLE page "
                    "ADD CONSTRAINT page_language_id_fkey "
                    "FOREIGN KEY (language_id) REFERENCES language(id) ON DELETE SET NULL; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; "
                    "END $$;"
                )
            )

    async def drop_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    # --- domain methods ---
    async def list_users(self, *, limit: int, offset: int) -> Tuple[list[UserRead], int]:
        """
        Deterministic paging by User.id (UUID) ASC.
        Returns (items, total).
        """
        limit = max(0, int(limit))
        offset = max(0, int(offset))

        async with self.session() as s:
            # total count
            total_stmt = select(func.count(User.id))
            total = int((await s.execute(total_stmt)).scalar_one())

            if limit == 0:
                return [], total

            # page items
            items_stmt = (
                select(User)
                .order_by(User.id.asc())         
                .limit(limit)
                .offset(offset)
            )
            rows: List[User] = (await s.execute(items_stmt)).scalars().all()

        return [UserRead.model_validate(r) for r in rows], total

    async def create_user(self, data: UserCreate) -> UserRead:
        """
        Create a user from UserCreate schema and return UserRead object.
        - All datetimes in the project are naive UTC (not applicable here).
        - On unique-constraint violation, re-raises IntegrityError for the caller to handle.
        """
        # Normalize role string -> enum if needed
        tg_username = None
        if data.tg_username:
            tg_username = data.tg_username[1:] if data.tg_username.startswith("@") else data.tg_username

        user = User(
            tg_username=tg_username,
            tg_id=data.tg_id,
            first_name=data.first_name,
            last_name=data.last_name,
            middle_name=data.middle_name,
            role=data.role,
            email=str(data.email) if data.email is not None else None,
            phone_number=data.phone_number,
            preferred_language_id=data.preferred_language_id,
        )

        async with self.session() as s:
            s.add(user)
            try:
                # commit happens in context manager; refresh after commit
                await s.flush()  # to get PK early if needed
            except IntegrityError:
                # rollback happens in context manager
                raise

            await s.refresh(user)

        return UserRead.model_validate(user)

    async def get_user(self, uid: Optional[uuid.UUID] = None, tg_id: Optional[int] = None, tg_username: Optional[str] = None) -> Optional[UserRead]:
        """
        Fetch a user record by one of several possible identifiers (priority order: id → tg_id → tg_username).

        The method performs sequential lookup attempts:
        1. Try to fetch by internal UUID (`get_user_by_id`);
        2. If not found, try to fetch by Telegram numeric ID (`get_user_by_tg_id`);
        3. If still not found, try by Telegram username (`get_user_by_tg_username`).

        Args:
            uid: Optional[uuid.UUID]
                Internal user UUID (primary key).
            tg_id: Optional[int]
                Telegram numeric user ID.
            tg_username: Optional[str]
                Telegram username (without '@').

        Returns:
            Optional[UserRead]:
                A `UserRead` DTO instance if the user exists; otherwise `None`.

        Notes:
            - Each lookup method returns immediately on success, preserving the priority order.
            - This is a convenience wrapper; it does not raise exceptions on missing input or missing user.
            - If multiple arguments are provided, only the first non-None matching criterion is used.
        """
        user = await self.get_user_by_id(uid)
        if user:
            return user

        user = await self.get_user_by_tg_id(tg_id)
        if user:
            return user

        user = await self.get_user_by_tg_username(tg_username)

        return user

    async def get_user_by_id(self, uid: Optional[uuid.UUID] = None) -> Optional[UserRead]:
        """
        Fetch a user by internal UUID primary key.

        Args:
            uid: User UUID. If None, returns None immediately.

        Returns:
            Optional[UserRead]: Pydantic DTO of the user if found; otherwise None.

        Notes:
            - Assumes `User.id` is the UUID primary key.
            - Uses a single SELECT ... WHERE id = :uid.
        """
        if uid is None:
            return None

        async with self.session() as s:
            stmt = select(User).where(User.id == uid)
            result = await s.execute(stmt)
            user_row = result.scalar_one_or_none()

        return UserRead.model_validate(user_row) if user_row is not None else None

    async def get_user_by_tg_id(self, tg_id: Optional[int] = None) -> Optional[UserRead]:
        """
        Fetch a user by Telegram numeric ID.

        Args:
            tg_id: Telegram user ID. If None, returns None immediately.

        Returns:
            Optional[UserRead]: Pydantic DTO of the user if found; otherwise None.

        Notes:
            - Assumes a unique constraint/index on `User.tg_id` for fast lookup.
        """
        if tg_id is None:
            return None

        async with self.session() as s:
            stmt = select(User).where(User.tg_id == tg_id)
            result = await s.execute(stmt)
            user_row = result.scalar_one_or_none()

        return UserRead.model_validate(user_row) if user_row is not None else None

    async def get_user_by_tg_username(self, tg_username: Optional[str] =  None) -> Optional[UserRead]:
        """
        Fetch a user by Telegram username (without the leading '@').

        Args:
            tg_username: Telegram username. If None or empty, returns None.
                Pass values like "john_doe" (no leading '@').

        Returns:
            Optional[UserRead]: Pydantic DTO of the user if found; otherwise None.
        """
        if not tg_username:
            return None

        # normalize optional leading '@'
        if tg_username.startswith("@"):
            tg_username = tg_username[1:]

        async with self.session() as s:
            stmt = select(User).where(User.tg_username == tg_username)
            result = await s.execute(stmt)
            user_row = result.scalar_one_or_none()

        return UserRead.model_validate(user_row) if user_row is not None else None

    async def update_user(self, data: UserUpdate) -> UserRead:
        """
        Partially update a user by id.
        Only fields explicitly provided (i.e., not Ellipsis) are updated.
        Passing None for a provided field will NULL it in DB.

        Args:
            data: UserUpdate DTO with `id` and optional fields. A field value of
                  `...` (Ellipsis) means "not provided, do not change".

        Returns:
            UserRead: updated user snapshot.

        Raises:
            LookupError: if the user with given id does not exist.
            IntegrityError: on unique constraint violation (e.g., tg_id, tg_username).
        """
        async with self.session() as s:
            db_user = await s.get(User, data.id)
            if db_user is None:
                raise LookupError("User not found.")

            def provided(v: object) -> bool:
                return v is not MISSING

            # Apply updates only for provided fields.
            if provided(data.tg_id):
                db_user.tg_id = data.tg_id  # may be None

            if provided(data.tg_username):
                if data.tg_username is None:
                    db_user.tg_username = None
                else:
                    username = data.tg_username[1:] if data.tg_username.startswith("@") else data.tg_username
                    db_user.tg_username = username

            if provided(data.first_name):
                db_user.first_name = data.first_name

            if provided(data.active_team_id):
                db_user.active_team_id = data.active_team_id

            if provided(data.last_name):
                db_user.last_name = data.last_name

            if provided(data.middle_name):
                db_user.middle_name = data.middle_name

            if provided(data.role):
                db_user.role = data.role  # UserRole enum

            if provided(data.email):
                db_user.email = str(data.email) if data.email is not None else None

            if provided(data.phone_number):
                db_user.phone_number = data.phone_number

            if provided(data.preferred_language_id):
                db_user.preferred_language_id = data.preferred_language_id

            if provided(data.ui_mode):
                db_user.ui_mode = data.ui_mode

            try:
                await s.flush()     # write pending changes (commit in ctx manager)
                await s.refresh(db_user)
            except IntegrityError:
                # rollback is handled by the context manager
                raise

        return UserRead.model_validate(db_user)

    async def upsert_user(self, data: UserCreate) -> UserRead:
        """
        Create or update a user identified by tg_id or tg_username (normalized, without '@').

        Semantics:
          - If user not found -> create (same mapping as in create_user).
          - If user exists -> update only non-null fields from payload
            (DO NOT change role on update).
          - Unique constraints on tg_id / tg_username are respected; IntegrityError is propagated.

        Returns:
          UserRead snapshot of the created/updated row.
        """
        # Normalize username once
        username = data.tg_username[1:] if data.tg_username and data.tg_username.startswith("@") else data.tg_username

        async with self.session() as s:
            # 1) Try by tg_id (preferred), then by username
            db_user = None
            if data.tg_id is not None:
                res = await s.execute(select(User).where(User.tg_id == data.tg_id))
                db_user = res.scalar_one_or_none()
            if db_user is None and username:
                res = await s.execute(select(User).where(User.tg_username == username))
                db_user = res.scalar_one_or_none()

            if db_user is None:
                # --- Create branch (align with create_user mapping) ---
                return await self.create_user(data)

            # --- Update branch (non-destructive; do not touch role) ---
            if username and db_user.tg_username != username:
                db_user.tg_username = username
            if data.tg_id is not None and db_user.tg_id != data.tg_id:
                db_user.tg_id = data.tg_id

            if data.first_name is not None:
                db_user.first_name = data.first_name
            if data.last_name is not None:
                db_user.last_name = data.last_name
            if data.middle_name is not None:
                db_user.middle_name = data.middle_name

            if data.email is not None:
                db_user.email = str(data.email)
            if data.phone_number is not None:
                db_user.phone_number = data.phone_number
            if data.preferred_language_id is not None:
                db_user.preferred_language_id = data.preferred_language_id
            if data.ui_mode is not None:
                db_user.ui_mode = data.ui_mode
            # active_team_id intentionally not mutated here

            await s.flush()
            await s.refresh(db_user)
            return UserRead.model_validate(db_user)

    async def get_language_by_id(self, lang_id: Optional[uuid.UUID] = None) -> Optional[LanguageRead]:
        """
        Fetch a language by its UUID primary key.

        Args:
            lang_id: UUID of the language. If None, returns None immediately.

        Returns:
            Optional[LanguageRead]:
                Pydantic DTO of the language if found; otherwise None.

        Notes:
            - Assumes `Language.id` is the UUID primary key.
            - Uses a single SELECT ... WHERE id = :lang_id.
        """
        if not lang_id:
            return None

        async with self.session() as s:
            stmt = select(Language).where(Language.id == lang_id)
            result = await s.execute(stmt)
            lang_row = result.scalar_one_or_none()

        return LanguageRead.model_validate(lang_row) if lang_row is not None else None

    async def get_language_by_name(self, language_name: Optional[str] = None) -> Optional[LanguageRead]:
        """
        Fetch a language by its name (case-insensitive).

        Args:
            language_name: The language name (e.g., "english", "russian").
                If None or empty, returns None.

        Returns:
            Optional[LanguageRead]:
                Pydantic DTO of the language if found; otherwise None.

        Notes:
            - Performs case-insensitive lookup via `ILIKE`.
            - Assumes `Language.name` has a unique constraint for consistent results.
        """
        if not language_name:
            return None

        name = language_name.strip()
        if not name:
            return None

        async with self.session() as s:
            stmt = select(Language).where(Language.name.ilike(name))
            result = await s.execute(stmt)
            lang_row = result.scalar_one_or_none()

        return LanguageRead.model_validate(lang_row) if lang_row is not None else None

    async def list_languages(self) -> list[LanguageRead]:
        """
        Return all languages ordered by name (case-insensitive).
        """
        async with self.session() as s:
            stmt = select(Language).order_by(Language.name.asc())
            res = await s.execute(stmt)
            rows = res.scalars().all()
        return [LanguageRead.model_validate(r) for r in rows]

    async def get_team(self, team_id: uuid.UUID) -> Optional[TeamRead]:
        """
        Fetch a team by its UUID.

        Args:
            team_id: Team primary key.

        Returns:
            Optional[TeamRead]: DTO if found; otherwise None.
        """
        if not team_id:
            return None

        async with self.session() as s:
            stmt = select(Team).where(Team.id == team_id)
            res = await s.execute(stmt)
            row = res.scalar_one_or_none()

        return TeamRead.model_validate(row) if row else None

    async def team_exists(self, team_id: uuid.UUID) -> bool:
        """
        Check whether a team with the given id exists.

        Args:
            team_id: Team primary key.

        Returns:
            bool: True if exists, False otherwise.
        """
        if not team_id:
            return False

        async with self.session() as s:
            stmt = select(Team.id).where(Team.id == team_id)
            res = await s.execute(stmt)
            return res.scalar_one_or_none() is not None

    async def list_teams(self, *, limit: int, offset: int) -> Tuple[list[TeamRead], int]:
        """Deterministic paging for teams ordered by title ASC, slug ASC."""
        limit = max(0, int(limit))
        offset = max(0, int(offset))

        async with self.session() as s:
            total_stmt = select(func.count(Team.id))
            total = int((await s.execute(total_stmt)).scalar_one())

            if limit == 0:
                return [], total

            items_stmt = (
                select(Team)
                .order_by(Team.title.asc(), Team.slug.asc(), Team.id.asc())
                .limit(limit)
                .offset(offset)
            )
            rows: List[Team] = (await s.execute(items_stmt)).scalars().all()

        return [TeamRead.model_validate(r) for r in rows], total

    async def create_team(self, payload: TeamCreate) -> TeamRead:
        """
        Create a new team.

        Args:
            payload: TeamCreate DTO.

        Returns:
            TeamRead: Created team snapshot.

        Raises:
            IntegrityError: On unique constraint violation (e.g., competition_id + slug).
        """
        team = Team(
            title=payload.title,
            slug=payload.slug,
            track_id=payload.track_id,
            error=payload.error,
        )

        async with self.session() as s:
            s.add(team)
            try:
                await s.flush()
                await s.refresh(team)
            except IntegrityError:
                # Context manager handles rollback.
                raise

        return TeamRead.model_validate(team)

    async def update_team(self, payload: TeamUpdate) -> TeamRead:
        """
        Partially update a team by id.

        Notes:
            Only fields explicitly provided (i.e., not Ellipsis) are updated.
            Passing None for a provided optional field writes NULL.

        Args:
            payload: TeamUpdate DTO with partial fields.

        Returns:
            TeamRead: Updated team snapshot.

        Raises:
            LookupError: If the team does not exist.
            IntegrityError: On unique/constraint violations.
        """
        team_id = payload.id
        async with self.session() as s:
            db_team = await s.get(Team, team_id)
            if db_team is None:
                raise LookupError("Team not found.")

            def provided(v: object) -> bool:
                return v is not MISSING

            if provided(payload.title):
                db_team.title = payload.title
            if provided(payload.slug):
                db_team.slug = payload.slug
            if provided(payload.error):
                db_team.error = payload.error

            try:
                await s.flush()
                await s.refresh(db_team)
            except IntegrityError:
                raise

        return TeamRead.model_validate(db_team)

    async def get_memberships_by_user(self, user_id: uuid.UUID) -> List[TeamUserRead]:
        """
        List all TeamUser memberships for a given user.

        Args:
            user_id: User UUID.

        Returns:
            list[TeamUserRead]: Memberships of the user (possibly empty).
        """
        async with self.session() as s:
            stmt = select(TeamUser).where(TeamUser.user_id == user_id)
            res = await s.execute(stmt)
            rows = res.scalars().all()
        return [TeamUserRead.model_validate(r) for r in rows]

    async def get_memberships_by_team(self, team_id: uuid.UUID) -> List[TeamUserRead]:
        """
        List all TeamUser memberships for a given team.

        Args:
            team_id: Team UUID.

        Returns:
            list[TeamUserRead]: Memberships of the team (possibly empty).
        """
        async with self.session() as s:
            stmt = select(TeamUser).where(TeamUser.team_id == team_id)
            res = await s.execute(stmt)
            rows = res.scalars().all()
        return [TeamUserRead.model_validate(r) for r in rows]

    async def get_membership_by_id(self, membership_id: uuid.UUID) -> TeamUserRead | None:
        """
        Fetch a TeamUser membership by its primary ID.

        Args:
            membership_id: TeamUser UUID.

        Returns:
            TeamUserRead | None: DTO if found; otherwise None.
        """
        async with self.session() as s:
            stmt = select(TeamUser).where(TeamUser.id == membership_id)
            res = await s.execute(stmt)
            row = res.scalar_one_or_none()
        return TeamUserRead.model_validate(row) if row else None

    async def get_membership(self, user_id: uuid.UUID, team_id: uuid.UUID) -> Optional[TeamUserRead]:
        """
        Fetch a TeamUser membership by (user_id, team_id).

        Args:
            user_id: User UUID.
            team_id: Team UUID.

        Returns:
            Optional[TeamUserRead]: DTO if found; otherwise None.
        """
        async with self.session() as s:
            stmt = select(TeamUser).where(
                TeamUser.user_id == user_id,
                TeamUser.team_id == team_id,
            )
            res = await s.execute(stmt)
            row = res.scalar_one_or_none()
        return TeamUserRead.model_validate(row) if row else None

    async def _set_active_team_if_none(
        self,
        s: AsyncSession,
        user_id: uuid.UUID,
        team_id: uuid.UUID,
    ) -> None:
        """
        If the user does not have an active team yet, set the given team as active.

        Notes:
            - This runs inside the provided session to keep the upsert operation atomic.
            - No-op if the user already has active_team_id set (non-NULL).

        Args:
            s: Existing async DB session (transactional context).
            user_id: Target user id.
            team_id: Team id to set as active when none is set.
        """
        db_user = await s.get(User, user_id)
        if db_user is None:
            # User not found; nothing to do.
            return
        if db_user.active_team_id is None:
            db_user.active_team_id = team_id
            await s.flush()  # persist the change within the same transaction

    async def upsert_membership(self, payload: TeamUserCreate | TeamUserUpdate) -> TeamUserRead:
        """
            Insert or update a TeamUser membership.

            Behavior:
                - If payload is TeamUserCreate and (user_id, team_id) is unique but exists,
                  role is updated (upsert).
                - If payload is TeamUserUpdate, updates the existing membership by id.
                - SIDE EFFECT: after upsert, if the user has no active team yet,
                  the just-(created/updated) team becomes user's active team.

            Args:
                payload: TeamUserCreate or TeamUserUpdate.

            Returns:
                TeamUserRead: Resulting membership snapshot.

            Raises:
                LookupError: If TeamUserUpdate.id does not exist.
                IntegrityError: On constraint violations during insert/update.
            """
        async with self.session() as s:
            if isinstance(payload, TeamUserCreate):
                db_m = TeamUser(
                    role=payload.role,
                    user_id=payload.user_id,
                    team_id=payload.team_id,
                )
                s.add(db_m)
                try:
                    await s.flush()
                    await s.refresh(db_m)
                    return TeamUserRead.model_validate(db_m)
                except IntegrityError:
                    # Unique (user_id, team_id) violated -> update role of existing row.
                    stmt = select(TeamUser).where(
                        TeamUser.user_id == payload.user_id,
                        TeamUser.team_id == payload.team_id,
                    )
                    res = await s.execute(stmt)
                    db_m = res.scalar_one()
                    db_m.role = payload.role

                    await self._set_active_team_if_none(s, db_m.user_id, db_m.team_id)
                    await s.flush()
                    await s.refresh(db_m)
                    return TeamUserRead.model_validate(db_m)

            # TeamUserUpdate branch
            db_m = await s.get(TeamUser, payload.id)
            if db_m is None:
                raise LookupError("TeamUser not found.")
            db_m.role = payload.role
            await s.flush()
            await self._set_active_team_if_none(s, db_m.user_id, db_m.team_id)
            await s.refresh(db_m)
            return TeamUserRead.model_validate(db_m)

    async def update_membership_role(self, user_id: uuid.UUID, team_id: uuid.UUID, role: ContestantRole) -> None:
        """
        Update a membership role by composite key (user_id, team_id).

        Args:
            user_id: User UUID.
            team_id: Team UUID.
            role: New ContestantRole value.

        Raises:
            LookupError: If the membership does not exist.
        """
        async with self.session() as s:
            stmt = select(TeamUser).where(
                TeamUser.user_id == user_id,
                TeamUser.team_id == team_id,
            )
            res = await s.execute(stmt)
            db_m = res.scalar_one_or_none()
            if db_m is None:
                raise LookupError("Membership not found.")
            db_m.role = role
            await s.flush()

    async def set_active_team(self, user_id: uuid.UUID, team_id: Optional[uuid.UUID]) -> None:
        """
        Set current active team for a user (or clear it with None).

        Args:
            user_id: User UUID.
            team_id: Team UUID to set as active; pass None to clear.

        Raises:
            LookupError: If the user does not exist.
        """
        async with self.session() as s:
            db_user = await s.get(User, user_id)
            if db_user is None:
                raise LookupError("User not found.")
            db_user.active_team_id = team_id
            await s.flush()

    # ---------- Submission: reads ----------

    async def get_submission_by_id(self, sub_id: uuid.UUID) -> Optional[SubmissionRead]:
        """
        Fetch a single submission by id.
        """
        if not sub_id:
            return None
        async with self.session() as s:
            db_obj = await s.get(Submission, sub_id)
            return SubmissionRead.model_validate(db_obj) if db_obj else None

    async def list_submissions_by_team(self, team_id: uuid.UUID) -> list[SubmissionRead]:
        """
        Return ALL submissions for a given team (no limit).
        Ordered by created_at desc if present, otherwise by id desc.
        """
        if not team_id:
            return []
        order_col = getattr(Submission, "created_at", Submission.id)
        async with self.session() as s:
            stmt = (
                select(Submission)
                .join(TeamUser, Submission.team_user_id == TeamUser.id)
                .where(TeamUser.team_id == team_id)
                .order_by(order_col.desc())
            )
            res = await s.execute(stmt)
            rows = res.scalars().all()
            return [SubmissionRead.model_validate(r) for r in rows]

    async def count_submissions_by_team(self, team_id: uuid.UUID) -> int:
        """
        Count submissions for a given team.
        """
        if not team_id:
            return 0
        async with self.session() as s:
            stmt = (
                select(func.count(Submission.id))
                .join(TeamUser, Submission.team_user_id == TeamUser.id)
                .where(TeamUser.team_id == team_id)
            )
            res = await s.execute(stmt)
            return int(res.scalar_one() or 0)

    async def bulk_submission_ids_by_team(self, team_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
        """
        For many team_ids return {team_id: [submission_id, ...]} in one round trip.
        Returns ALL ids per team; recent-first.
        """
        mapping: dict[uuid.UUID, list[uuid.UUID]] = {tid: [] for tid in team_ids if tid}
        if not mapping:
            return mapping

        order_col = getattr(Submission, "created_at", Submission.id)
        keys = list(mapping.keys())
        async with self.session() as s:
            stmt = (
                select(TeamUser.team_id, Submission.id)
                .join(TeamUser, Submission.team_user_id == TeamUser.id)
                .where(TeamUser.team_id.in_(keys))
                .order_by(order_col.desc())
            )
            res = await s.execute(stmt)
            for team_id, sub_id in res.all():
                if team_id in mapping:
                    mapping[team_id].append(sub_id)
        return mapping

    async def list_submissions_page(
        self,
        *,
        limit: int,
        offset: int,
        status: SubmissionStatus | None = None,
    ) -> Tuple[list[SubmissionRead], int]:
        """
        Paginated submissions listing optionally filtered by status.
        Ordered by created_at desc (fallback to id desc).
        """
        limit = max(0, int(limit))
        offset = max(0, int(offset))
        order_col = getattr(Submission, "created_at", Submission.id)

        async with self.session() as s:
            total_stmt = select(func.count(Submission.id))
            items_stmt = (
                select(Submission)
                .order_by(order_col.desc(), Submission.id.desc())
                .limit(limit)
                .offset(offset)
            )
            if status is not None:
                total_stmt = total_stmt.where(Submission.status == status)
                items_stmt = items_stmt.where(Submission.status == status)

            total = int((await s.execute(total_stmt)).scalar_one())
            if limit == 0:
                return [], total

            rows = (await s.execute(items_stmt)).scalars().all()

        return [SubmissionRead.model_validate(r) for r in rows], total

    # ---------- Submission: writes ----------

    async def create_submission(self, data: SubmissionCreate) -> SubmissionRead:
        """
        Insert a new submission row.
        """
        async with self.session() as s:
            db_obj = Submission(
                team_user_id=data.team_user_id,
                title=data.title,
                file_path=data.file_path,
                value=data.value,
                status=data.status,
            )
            s.add(db_obj)
            # Commit in context manager; ensure we have generated values:
            await s.flush()
            await s.refresh(db_obj)
            return SubmissionRead.model_validate(db_obj)

    async def update_submission(self, data: SubmissionUpdate) -> SubmissionRead:
        """
        Update an existing submission by id (partial update).
        """
        async with self.session() as s:
            db_obj = await s.get(Submission, data.id)
            if db_obj is None:
                raise LookupError("Submission not found.")

            # Apply only provided fields
            if data.value is not None:
                db_obj.value = data.value
            if data.status is not None:
                db_obj.status = data.status
            if data.title is not None:
                db_obj.title = data.title

            await s.flush()
            await s.refresh(db_obj)
            return SubmissionRead.model_validate(db_obj)

    async def upsert_submission(self, payload: SubmissionCreate | SubmissionUpdate) -> SubmissionRead:
        """
        Delegate to create_submission / update_submission based on payload type.
        """
        if isinstance(payload, SubmissionCreate):
            return await self.create_submission(payload)
        else:
            return await self.update_submission(payload)

    async def get_competition_by_id(self, competition_id: uuid.UUID) -> Optional[CompetitionRead]:
            """Fetch a competition by its UUID."""
            if not competition_id:
                return None
            async with self.session() as s:
                stmt = select(Competition).where(Competition.id == competition_id)
                res = await s.execute(stmt)
                row = res.scalar_one_or_none()
            return CompetitionRead.model_validate(row) if row is not None else None

    async def get_competition_by_slug(self, slug: str) -> Optional[CompetitionRead]:
            """Fetch a competition by its slug (case-insensitive)."""
            if not slug:
                return None
            name = slug.strip()
            if not name:
                return None
            async with self.session() as s:
                stmt = select(Competition).where(Competition.slug.ilike(name))
                res = await s.execute(stmt)
                row = res.scalar_one_or_none()
            return CompetitionRead.model_validate(row) if row is not None else None

    async def list_competitions(self, *, limit: int, offset: int) -> Tuple[list[CompetitionRead], int]:
            """Deterministic paging for competitions (start_at ASC, then title ASC)."""
            limit = max(0, int(limit))
            offset = max(0, int(offset))

            async with self.session() as s:
                total_stmt = select(func.count(Competition.id))
                total = int((await s.execute(total_stmt)).scalar_one())

                if limit == 0:
                    return [], total

                items_stmt = (
                    select(Competition)
                    .order_by(Competition.start_at.asc(), Competition.title.asc(), Competition.id.asc())
                    .limit(limit)
                    .offset(offset)
                )
                rows: List[Competition] = (await s.execute(items_stmt)).scalars().all()

            return [CompetitionRead.model_validate(r) for r in rows], total

    async def create_competition(self, payload: CompetitionCreate) -> CompetitionRead:
            """Create a new competition."""
            obj = Competition(
                title=payload.title,
                start_at=payload.start_at,
                end_at=payload.end_at,
                slug=payload.slug,
            )
            async with self.session() as s:
                s.add(obj)
                try:
                    await s.flush()
                except IntegrityError:
                    raise
                await s.refresh(obj)
                return CompetitionRead.model_validate(obj)

    async def update_competition(self, payload: CompetitionUpdate) -> CompetitionRead:
            """Partially update a competition by id."""
            def provided(v: object) -> bool:
                return v is not MISSING
            async with self.session() as s:
                db_obj = await s.get(Competition, payload.id)
                if db_obj is None:
                    raise LookupError("Competition not found.")
                if provided(payload.title):
                    db_obj.title = payload.title
                if provided(payload.start_at):
                    db_obj.start_at = payload.start_at
                if provided(payload.end_at):
                    db_obj.end_at = payload.end_at
                if provided(payload.slug):
                    db_obj.slug = payload.slug
                await s.flush()
                await s.refresh(db_obj)
                return CompetitionRead.model_validate(db_obj)

    async def get_track_by_id(self, track_id: uuid.UUID) -> Optional[TrackRead]:
            """Fetch a track by its UUID."""
            if not track_id:
                return None
            async with self.session() as s:
                stmt = select(Track).where(Track.id == track_id)
                res = await s.execute(stmt)
                row = res.scalar_one_or_none()
            return TrackRead.model_validate(row) if row is not None else None

    async def list_tracks_by_competition(self, competition_id: uuid.UUID) -> list[TrackRead]:
            """List all tracks for a given competition."""
            if not competition_id:
                return []
            async with self.session() as s:
                stmt = select(Track).where(Track.competition_id == competition_id)
                res = await s.execute(stmt)
                rows = res.scalars().all()
            return [TrackRead.model_validate(r) for r in rows]

    async def create_track(self, payload: TrackCreate) -> TrackRead:
            """Create a new track."""
            obj = Track(
                slug=payload.slug,
                title=payload.title,
                competition_id=payload.competition_id,
                max_contestants=payload.max_contestants,
                max_submissions_total=payload.max_submissions_total,
                sort_by=payload.sort_by,
            )
            async with self.session() as s:
                s.add(obj)
                try:
                    await s.flush()
                except IntegrityError:
                    raise
                await s.refresh(obj)
                return TrackRead.model_validate(obj)

    async def update_track(self, payload: TrackUpdate) -> TrackRead:
            """Partially update a track by id."""
            def provided(v: object) -> bool:
                return v is not MISSING
            async with self.session() as s:
                db_obj = await s.get(Track, payload.id)
                if db_obj is None:
                    raise LookupError("Track not found.")
                if provided(payload.slug):
                    db_obj.slug = payload.slug
                if provided(payload.title):
                    db_obj.title = payload.title
                if provided(payload.max_submissions_total):
                    db_obj.max_submissions_total = payload.max_submissions_total
                if provided(payload.sort_by):
                    db_obj.sort_by = payload.sort_by
                await s.flush()
                await s.refresh(db_obj)
                return TrackRead.model_validate(db_obj)

    async def get_page_by_id(self, page_id: uuid.UUID) -> Optional[PageRead]:
        """Fetch a page by its UUID."""
        if not page_id:
            return None
        async with self.session() as s:
            stmt = select(Page).where(Page.id == page_id)
            res = await s.execute(stmt)
            row = res.scalar_one_or_none()
        return PageRead.model_validate(row) if row is not None else None

    async def leaderboard_for_track(self, track_id: uuid.UUID, direction: SortDirection) -> list[TrackLeaderboardRow]:
        """Return leaderboard rows for a track, ordered per sort direction."""
        if not track_id:
            return []
        best_value_func = func.max if direction == SortDirection.DESC else func.min
        base_subquery = (
            select(
                Team.id.label("team_id"),
                Team.title.label("team_title"),
                func.count(Submission.id).label("submission_count"),
                best_value_func(Submission.value).label("best_value"),
            )
            .select_from(Team)
            .join(TeamUser, TeamUser.team_id == Team.id, isouter=True)
            .join(
                Submission,
                and_(Submission.team_user_id == TeamUser.id, Submission.status == SubmissionStatus.ACCEPTED),
                isouter=True,
            )
            .where(Team.track_id == track_id)
            .group_by(Team.id, Team.title)
        ).subquery()

        best_created_expr = func.min(Submission.created_at)
        async with self.session() as s:
            stmt = (
                select(
                    base_subquery.c.team_id,
                    base_subquery.c.team_title,
                    base_subquery.c.submission_count,
                    base_subquery.c.best_value,
                    best_created_expr.label("best_created_at"),
                )
                .join(TeamUser, TeamUser.team_id == base_subquery.c.team_id)
                .join(
                    Submission,
                    and_(Submission.team_user_id == TeamUser.id, Submission.status == SubmissionStatus.ACCEPTED),
                )
                .where(
                    Submission.value == base_subquery.c.best_value,
                    TeamUser.team_id == base_subquery.c.team_id,
                )
                .group_by(
                    base_subquery.c.team_id,
                    base_subquery.c.team_title,
                    base_subquery.c.submission_count,
                    base_subquery.c.best_value,
                )
                .order_by(
                    (
                        base_subquery.c.best_value.desc().nullslast()
                        if direction == SortDirection.DESC
                        else base_subquery.c.best_value.asc().nullslast()
                    ),
                    best_created_expr.asc(),
                    base_subquery.c.team_title.asc(),
                )
            )
            rows = (await s.execute(stmt)).all()
        return [
            TrackLeaderboardRow(
                team_id=row.team_id,
                team_title=row.team_title,
                best_value=row.best_value,
                submission_count=int(row.submission_count or 0),
                best_created_at=row.best_created_at,
            )
            for row in rows
        ]

    async def get_page_by_slug(self, competition_id: uuid.UUID, slug: str, language_id: Optional[uuid.UUID] = None) -> Optional[PageRead]:
            """Fetch a page by (competition_id, slug, language)."""
            if not competition_id or not slug:
                return None
            name = slug.strip()
            if not name:
                return None
            async with self.session() as s:
                stmt = select(Page).where(Page.competition_id == competition_id, Page.slug.ilike(name))
                if language_id is not None:
                    stmt = stmt.where(Page.language_id == language_id)
                else:
                    stmt = stmt.where(Page.language_id.is_(None))
                res = await s.execute(stmt)
                row = res.scalar_one_or_none()
            return PageRead.model_validate(row) if row is not None else None

    async def list_pages_by_competition(self, competition_id: uuid.UUID) -> list[PageRead]:
            """List pages for a competition."""
            if not competition_id:
                return []
            async with self.session() as s:
                stmt = select(Page).where(Page.competition_id == competition_id)
                res = await s.execute(stmt)
                rows = res.scalars().all()
            return [PageRead.model_validate(r) for r in rows]

    async def list_pages_by_track(self, track_id: uuid.UUID) -> list[PageRead]:
            """List pages for a track."""
            if not track_id:
                return []
            async with self.session() as s:
                stmt = select(Page).where(Page.track_id == track_id)
                res = await s.execute(stmt)
                rows = res.scalars().all()
            return [PageRead.model_validate(r) for r in rows]

    async def create_page(self, payload: PageCreate) -> PageRead:
            """Create a new page."""
            obj = Page(
                title=payload.title,
                slug=payload.slug,
                type=payload.type,
                file_basename=payload.file_basename,
                competition_id=payload.competition_id,
                track_id=payload.track_id,
                language_id=payload.language_id,
            )
            async with self.session() as s:
                s.add(obj)
                try:
                    await s.flush()
                except IntegrityError:
                    raise
                await s.refresh(obj)
                return PageRead.model_validate(obj)

    async def update_page(self, payload: PageUpdate) -> PageRead:
            """Partially update a page by id."""
            def provided(v: object) -> bool:
                return v is not MISSING

            async with self.session() as s:
                db_obj = await s.get(Page, payload.id)
                if db_obj is None:
                    raise LookupError("Page not found.")

                if provided(payload.title):
                    db_obj.title = payload.title
                if provided(payload.slug):
                    db_obj.slug = payload.slug
                if provided(payload.type):
                    db_obj.type = payload.type
                if provided(payload.file_basename):
                    db_obj.file_basename = payload.file_basename
                if provided(payload.language_id):
                    db_obj.language_id = payload.language_id

                await s.flush()
                await s.refresh(db_obj)
                return PageRead.model_validate(db_obj)

    # ---------------------------------
    # Audit log helpers
    # ---------------------------------

    async def create_audit_log(self, payload: AuditLogCreate) -> AuditLogRead:
        """Persist a new audit log entry."""
        async with self.session() as s:
            record = AuditLog(
                actor_id=payload.actor_id,
                action=payload.action,
                payload=dict(payload.payload or {}),
            )
            s.add(record)
            await s.flush()
            await s.refresh(record)
            return AuditLogRead.model_validate(record)

    async def list_audit_logs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
    ) -> tuple[list[AuditLogRead], int]:
        """Return paginated audit log entries filtered by actor/action."""
        limit = max(0, int(limit))
        offset = max(0, int(offset))

        async with self.session() as s:
            stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
            count_stmt = select(func.count(AuditLog.id))
            if actor_id:
                stmt = stmt.where(AuditLog.actor_id == actor_id)
                count_stmt = count_stmt.where(AuditLog.actor_id == actor_id)
            if action:
                stmt = stmt.where(AuditLog.action == action)
                count_stmt = count_stmt.where(AuditLog.action == action)

            if limit:
                stmt = stmt.limit(limit)
            if offset:
                stmt = stmt.offset(offset)

            rows = (await s.execute(stmt)).scalars().all()
            total = int((await s.execute(count_stmt)).scalar_one())

        return [AuditLogRead.model_validate(row) for row in rows], total
