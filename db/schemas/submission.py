# db/schemas/submission.py
import uuid
from datetime import datetime
from smart_solution.db.schemas._base import OrmModel
from smart_solution.db.enums import SubmissionStatus

class SubmissionBase(OrmModel):
    team_user_id: uuid.UUID
    title: str
    file_path: str
    value: float | None = None
    status: SubmissionStatus = SubmissionStatus.PENDING

class SubmissionCreate(SubmissionBase): ...
class SubmissionRead(SubmissionBase):
    id: uuid.UUID
    created_at: datetime

class SubmissionUpdate(OrmModel):
    id: uuid.UUID
    value: float | None = None
    status: SubmissionStatus | None = None
    title: str | None = None
