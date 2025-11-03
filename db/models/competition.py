# db/models/competition.py
import uuid
from datetime import datetime
from typing import List
from sqlalchemy import CheckConstraint, DateTime, String, Integer, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from smart_solution.db.models._base import Base

class Competition(Base):
    __tablename__ = "competition"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    pages: Mapped[List["Page"]] = relationship(back_populates="competition", cascade="all, delete-orphan", passive_deletes=True)
