# db/models/team.py
import uuid
from typing import List, Optional
from sqlalchemy import ForeignKey, Index, String, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from smart_solution.db.models._base import Base

class Team(Base):
    __tablename__ = "team"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    error: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    track_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("track.id", ondelete="SET NULL"), nullable=True
    )

    track: Mapped[Optional["Track"]] = relationship(back_populates="teams")

    team_users: Mapped[List["TeamUser"]] = relationship(
        back_populates="team", cascade="all, delete-orphan", passive_deletes=True
    )
