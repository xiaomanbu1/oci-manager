"""
入口：同一个 asyncio loop 里同时跑 FastAPI(uvicorn) 和 Telegram 机器人。
    python -m app.main
"""
import asyncio
import logging
import signal

import uvicorn

from .config import load_config
from .service import Service
from .web import create_app
from .bot import OciBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("oci_manager")


async def main():
    config = load_config()
    service = Service(config)

    if not service.account_names():
        log.warning("没有任何账号初始化成功，请检查 config.yaml")

    tasks = []
    bot = None
    server = None

    # Web
    if config.web.enabled:
        app = create_app(service, password=config.web.password)
        uconf = uvicorn.Config(app, host=config.web.host, port=config.web.port,
                               log_level="warning")
        server = uvicorn.Server(uconf)
        tasks.append(asyncio.create_task(server.serve()))
        log.info("Web 面板: http://%s:%s", config.web.host, config.web.port)

    # Telegram
    if config.telegram.enabled and config.telegram.token:
        bot = OciBot(service, config.telegram.token, config.telegram.admin_ids)
        await bot.start()
    elif config.telegram.enabled:
        log.warning("Telegram 已启用但缺 token，跳过")

    if not tasks and not bot:
        log.error("Web 和 Bot 都没启用，退出")
        return

    # 优雅退出
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await stop.wait()
    log.info("正在关闭…")
    if bot:
        await bot.stop()
    if server:
        server.should_exit = True
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
