# db/models/user.py
import uuid
from typing import List, Optional
from sqlalchemy import Enum as SAEnum, Index, String, Integer, UniqueConstraint, ForeignKey, BigInteger
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from smart_solution.db.models._base import Base
from smart_solution.db.enums import UserRole, UiMode

class User(Base):
    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    tg_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    middle_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole, name="user_role"), nullable=False, default=UserRole.CONTESTANT)
    email: Mapped[Optional[str]] = mapped_column(String(254), nullable=True)
    phone_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    active_team_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("team.id", ondelete="SET NULL"), nullable=True)
    ui_mode: Mapped[UiMode] = mapped_column(SAEnum(UiMode, name="ui_mode"), nullable=False, default=UiMode.HOME)

    preferred_language_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("language.id", ondelete="SET NULL"), nullable=True, index=True
    )

    preferred_language: Mapped[Optional["Language"]] = relationship("Language")
    memberships: Mapped[List["TeamUser"]] = relationship(back_populates="user", passive_deletes=True)
