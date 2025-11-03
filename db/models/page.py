# db/models/page.py
import uuid
from sqlalchemy import Enum as SAEnum, Index, String, UniqueConstraint, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from smart_solution.db.models._base import Base
from smart_solution.db.enums import PageType
from smart_solution.db.models.language import Language

class Page(Base):
    __tablename__ = "page"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    file_basename: Mapped[str] = mapped_column(String(256), nullable=False)
    type: Mapped[PageType] = mapped_column(SAEnum(PageType, name="page_type"), nullable=False, default=PageType.CUSTOM)

    competition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("competition.id", ondelete="CASCADE"), nullable=True
    )
    track_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("track.id", ondelete="CASCADE"), nullable=True
    )
    language_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("language.id", ondelete="SET NULL"), nullable=True, index=True
    )

    competition: Mapped["Competition"] = relationship(back_populates="pages")
    language: Mapped[Language] = relationship()
