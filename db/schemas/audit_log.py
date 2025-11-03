# db/schemas/audit_log.py
import uuid
from datetime import datetime
from typing import Optional
from ._base import OrmModel

class AuditLogBase(OrmModel):
    actor_id: Optional[uuid.UUID] = None
    action: str
    payload: dict = {}

class AuditLogCreate(AuditLogBase): ...
class AuditLogRead(AuditLogBase):
    id: uuid.UUID
    created_at: datetime
