# db/models/track.py
import uuid
from typing import List
from sqlalchemy import String, UniqueConstraint, ForeignKey, Integer, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from smart_solution.db.models._base import Base
from smart_solution.db.enums import SortDirection

class Track(Base):
    __tablename__ = "track"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    max_submissions_total: Mapped[Integer] = mapped_column(Integer, default=100)
    max_contestants: Mapped[Integer] = mapped_column(Integer, default=3)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    competition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("competition.id", ondelete="CASCADE"),
        nullable=False,
    )
    sort_by: Mapped[SortDirection] = mapped_column(
        SAEnum(SortDirection, name="sort_direction"),
        nullable=False,
        default=SortDirection.DESC,
    )

    teams: Mapped[List["Team"]] = relationship(back_populates="track", passive_deletes=True)
