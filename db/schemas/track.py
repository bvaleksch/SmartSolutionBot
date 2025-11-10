# db/schemas/track.py
import uuid
from datetime import datetime
from typing import Optional
from smart_solution.db.enums import SortDirection
from smart_solution.db.schemas._base import OrmModel
from smart_solution.utils.sentinels import Missing 

class TrackBase(OrmModel):
    title: str
    slug: str
    competition_id: uuid.UUID
    sort_by: SortDirection = SortDirection.DESC
    max_contestants: int = 3
    max_submissions_total: int

class TrackCreate(TrackBase): ...
class TrackUpdate(OrmModel):
    id: uuid.UUID
    sort_by: SortDirection | Missing = Missing()
    slug: str | Missing = Missing()
    title: str | Missing | None = Missing()
    max_submissions_total: int | Missing = Missing()

class TrackRead(TrackBase):
    id: uuid.UUID

class ShortTrackInfo(OrmModel):
    slug: str
    competition_title: str
    track_title: str
    start_at: datetime
    end_at: datetime

class TrackLeaderboardRow(OrmModel):
    best_created_at: datetime | None = None
    team_id: uuid.UUID
    team_title: str
    best_value: float | None = None
    submission_count: int = 0
