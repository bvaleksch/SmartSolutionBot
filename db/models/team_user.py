# db/models/team_user.py
import uuid
from sqlalchemy import Enum as SAEnum, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from smart_solution.db.models._base import Base
from smart_solution.db.enums import ContestantRole

class TeamUser(Base):
    __tablename__ = "team_user"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role: Mapped[ContestantRole] = mapped_column(SAEnum(ContestantRole, name="contestant_role"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("team.id", ondelete="CASCADE"), nullable=False)

    user = relationship("User", back_populates="memberships")
    team = relationship("Team", back_populates="team_users")

    submissions = relationship("Submission", back_populates="team_user", cascade="all, delete-orphan", passive_deletes=True)

