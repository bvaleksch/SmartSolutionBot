# bot/routers/utils.py
from smart_solution.i18n import Localizer
from smart_solution.bot.services.language import LanguageService
from smart_solution.db.schemas.user import UserRead

async def get_localizer_by_user(user: UserRead) -> Localizer:
    lng_svc = LanguageService()
    lang = await lng_svc.safe_autoget(user.preferred_language_id)
    return Localizer(lang.name)
