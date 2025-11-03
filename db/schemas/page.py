# db/schemas/page.py
import uuid
from typing import Optional
from smart_solution.db.schemas._base import OrmModel
from smart_solution.db.enums import PageType
from smart_solution.utils.sentinels import Missing

class PageBase(OrmModel):
    title: str
    slug: str
    type: PageType
    file_basename: str
    competition_id: Optional[uuid.UUID] = None
    track_id: Optional[uuid.UUID] = None
    language_id: Optional[uuid.UUID] = None

class PageCreate(PageBase): ...
class PageUpdate(OrmModel):
    id: uuid.UUID
    title: str | Missing = Missing()
    slug: str | Missing = Missing()
    type: PageType | Missing = Missing()
    file_basename: str | Missing = Missing()
    language_id: uuid.UUID | Missing = Missing()

class PageRead(PageBase):
    id: uuid.UUID
