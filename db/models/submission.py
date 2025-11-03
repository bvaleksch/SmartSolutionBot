# db/models/submission.py
import uuid
from datetime import datetime
from sqlalchemy import Enum as SAEnum, DateTime, ForeignKey, Index, String, text, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from smart_solution.db.models._base import Base
from smart_solution.db.enums import SubmissionStatus

class Submission(Base):
    __tablename__ = "submission"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("team_user.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=text("(NOW() AT TIME ZONE 'UTC')")
    )
    value: Mapped[Numeric | None] = mapped_column(Numeric(10, 4), nullable=True)
    status: Mapped[SubmissionStatus] = mapped_column(
        SAEnum(SubmissionStatus, name="submission_status"),
        nullable=False,
        default=SubmissionStatus.PENDING,
    )

    team_user = relationship("TeamUser")
