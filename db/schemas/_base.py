# db/schemas/_base.py
from pydantic import BaseModel, ConfigDict

class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
