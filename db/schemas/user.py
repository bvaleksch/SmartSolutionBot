# app/db/schemas/user.py
import uuid
from typing import Optional
from pydantic import EmailStr, Field
from smart_solution.db.schemas._base import OrmModel
from smart_solution.db.enums import UserRole, UiMode
from smart_solution.utils.sentinels import Missing

class UserBase(OrmModel):
    tg_id: Optional[int] = None
    tg_username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    middle_name: Optional[str] = None
    role: UserRole = "unregistered"
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = None
    preferred_language_id: Optional[uuid.UUID] = None
    active_team_id: Optional[uuid.UUID] = None
    ui_mode: UiMode = UiMode.HOME

class UserCreate(UserBase):
    tg_username: str
    
class UserUpdate(OrmModel):
    id: uuid.UUID
    tg_id: int | Missing | None = Missing()
    tg_username: str | Missing | None = Missing()
    first_name: str | Missing | None = Missing()
    last_name: str | Missing | None = Missing()
    middle_name: str | Missing | None = Missing()
    role: UserRole | Missing = Missing()
    email: EmailStr | Missing = Missing()
    phone_number: str | Missing = Missing()
    preferred_language_id: uuid.UUID | Missing = Missing()
    active_team_id: uuid.UUID | Missing = Missing()
    ui_mode: UiMode | Missing = Missing()

class UserRead(UserBase):
    id: uuid.UUID

