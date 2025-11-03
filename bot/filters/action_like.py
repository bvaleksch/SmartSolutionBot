# bot/filters/action_like.py
from fnmatch import fnmatchcase
from aiogram.filters import BaseFilter
from typing import Optional, Any

class ActionLike(BaseFilter):
    def __init__(self, *patterns: str):
        # store lowercased patterns
        self.patterns = tuple(p.lower() for p in patterns)

    async def __call__(self, event: Any, ui_action: Optional[str] = None, **kwargs) -> bool:
        if not ui_action:
            return False
        action = ui_action.lower()
        return any(fnmatchcase(action, pat) for pat in self.patterns)
