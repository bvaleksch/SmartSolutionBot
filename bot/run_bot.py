# bot/run_bot.py
import asyncio
import logging

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.telegram import TelegramAPIServer
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from smart_solution.config import Settings
from smart_solution.bot.middlewares.user import UserMiddleware
from smart_solution.bot.middlewares.rate_limit import RateLimitMiddleware
from smart_solution.bot.middlewares.whitelist import WhitelistMiddleware
from smart_solution.bot.middlewares.action import ActionMiddleware
from smart_solution.bot.routers.core import router as CoreRouter
from smart_solution.bot.routers.users import router as UserRouter
from smart_solution.bot.routers.competitions import router as CompetitionRouter
from smart_solution.bot.routers.teams import router as TeamRouter
from smart_solution.bot.routers.team_members import router as TeamMemberRouter
from smart_solution.bot.routers.team_user_mode import router as TeamUserRouter
from smart_solution.bot.routers.submissions_user import router as SubmissionUserRouter
from smart_solution.bot.routers.submissions_admin import router as SubmissionAdminRouter
from smart_solution.db.database import DataBase
from smart_solution.bot.services.submission_notifications import submission_notifier
import smart_solution.bot.services.auto_judge_first_track  # noqa: F401

logging.basicConfig(level=logging.INFO)


def setup_dispatcher(dp: Dispatcher) -> None:
    dp.update.outer_middleware(UserMiddleware())
    dp.update.outer_middleware(WhitelistMiddleware())
    dp.update.outer_middleware(RateLimitMiddleware())
    dp.message.outer_middleware(ActionMiddleware())

def setup_routers(dp: Dispatcher) -> None:
    dp.include_router(CoreRouter)
    dp.include_router(UserRouter)
    dp.include_router(CompetitionRouter)
    dp.include_router(TeamRouter)
    dp.include_router(TeamMemberRouter)
    dp.include_router(TeamUserRouter)
    dp.include_router(SubmissionAdminRouter)
    dp.include_router(SubmissionUserRouter)

async def main() -> None:
    settings = Settings()
    BOT_TOKEN = settings.bot_token

    if not BOT_TOKEN:
        raise RuntimeError("Bot token is not set.")

    session = AiohttpSession(api=TelegramAPIServer.from_base("http://127.0.0.1:8081", is_local=True))
    bot = Bot(
        BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            # link_preview_is_disabled=True,
            # protect_content=True,
        ),
    )

    dp = Dispatcher()
    setup_dispatcher(dp)
    setup_routers(dp)

    submission_notifier.bind_bot(bot)

    await DataBase().create_all()

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
