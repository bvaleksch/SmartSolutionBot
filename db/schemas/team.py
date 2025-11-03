# db/schemas/team.py
import uuid
from typing import Optional
from smart_solution.db.schemas._base import OrmModel
from smart_solution.utils.sentinels import Missing

class TeamBase(OrmModel):
    title: str
    slug: str
    track_id: Optional[uuid.UUID] = None
    error: Optional[str] = None

class TeamCreate(TeamBase): ...
class TeamUpdate(OrmModel):
    id: uuid.UUID
    title: str | Missing = Missing()
    slug: str | Missing = Missing()
    error: str | Missing | None = Missing()

class TeamRead(TeamBase):
    id: uuid.UUID
