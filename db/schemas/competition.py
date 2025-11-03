# db/schemas/competition.py
import uuid
from datetime import datetime
from typing import Optional
from smart_solution.db.schemas._base import OrmModel
from smart_solution.utils.sentinels import Missing

class CompetitionBase(OrmModel):
    title: str
    start_at: datetime
    end_at: datetime
    slug: str

class CompetitionCreate(CompetitionBase): ...
class CompetitionUpdate(OrmModel):
    title: str | Missing = Missing()
    start_at: datetime | Missing = Missing()
    end_at: datetime | Missing = Missing()
    slug: str | Missing = Missing()

class CompetitionRead(CompetitionBase):
    id: uuid.UUID
