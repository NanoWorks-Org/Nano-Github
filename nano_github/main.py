from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn

from nano_github.config import settings
from nano_github.database import Database
from nano_github.discord_bot import NanoGitHubBot
from nano_github.github_webhook import create_app


def configure_logging() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


async def run() -> None:
    configure_logging()
    db = Database(settings.sqlite_path)
    db.init()

    bot = NanoGitHubBot(db)
    app = create_app(settings, db, bot)
    uvicorn_config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(uvicorn_config)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    api_task = asyncio.create_task(server.serve(), name="nano-github-api")
    bot_task = asyncio.create_task(bot.start(settings.discord_token), name="nano-github-discord")

    done, pending = await asyncio.wait(
        {api_task, bot_task, asyncio.create_task(stop_event.wait())},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in done:
        if task is not api_task and task is not bot_task:
            continue
        exception = task.exception()
        if exception:
            raise exception

    server.should_exit = True
    await bot.close()

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    db.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

