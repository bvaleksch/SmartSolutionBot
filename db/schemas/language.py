# app/db/schemas/language.py
import uuid
from typing import Optional
from smart_solution.db.schemas._base import OrmModel

class LanguageBase(OrmModel):
    name: str
    title: str

class LanguageCreate(LanguageBase): ...
class LanguageUpdate(OrmModel):
    name: Optional[str] = None
    title: Optional[str] = None

class LanguageRead(LanguageBase):
    id: uuid.UUID
