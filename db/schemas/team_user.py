# app/db/schemas/team_user.py
import uuid
from smart_solution.db.schemas._base import OrmModel
from smart_solution.db.enums import ContestantRole


class TeamUserBase(OrmModel):
    role: ContestantRole
    user_id: uuid.UUID
    team_id: uuid.UUID

class TeamUserCreate(TeamUserBase): ...
class TeamUserUpdate(OrmModel):
    id: uuid.UUID   
    role: ContestantRole

class TeamUserRead(TeamUserBase):
    id: uuid.UUID

    def __hash__(self) -> int:
        return hash(self.id)
