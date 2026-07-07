"""
VCM エントリポイント。FastAPI(uvicorn) を起動し、トークンがあれば
discord.py クライアントも同一の asyncio イベントループで起動する。

トークンが未設定でも GUI は開き、ブラウザからトークンを設定できる。
設定はすべて config.json（vcm/config.py 参照）。

    python -m vcm.main
"""

import asyncio
import logging
import os
import webbrowser

import uvicorn

from vcm import config as vcm_config
from vcm import update as vcm_update
from vcm.runner import BotRunner
from vcm.server import ConnectionManager, create_app
from vcm.voicevox import VoicevoxEngine


async def main():
    # discord.py の警告・音声まわりのログをコンソールに出す
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord.voice_state").setLevel(logging.INFO)
    logging.getLogger("discord.player").setLevel(logging.INFO)

    default_guild_id = vcm_config.get_guild_id()
    host = vcm_config.get_host()
    port = vcm_config.get_port()

    manager = ConnectionManager()
    manager.default_guild_id = default_guild_id
    manager.migrate_legacy_presets()

    engine = VoicevoxEngine(
        on_change=manager.broadcast,
        config_path=vcm_config.get_voicevox_path(),
    )
    manager.engine = engine

    runner = BotRunner(manager, default_guild_id=default_guild_id, engine=engine)
    manager.runner = runner
    app = create_app(manager, runner)

    config = uvicorn.Config(app, host=host, port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    async def request_shutdown():
        print("[VCM] GUI が閉じられました。終了します…")
        server.should_exit = True

    manager.set_shutdown(request_shutdown)

    token = vcm_config.get_token()
    if token:
        startup = asyncio.create_task(runner.start(token))
    else:
        startup = None
        print("[VCM] トークン未設定。ブラウザの GUI から Bot トークンを設定してください。")

    engine.start_detection()

    async def open_browser_when_ready():
        """サーバーが待ち受けを開始してからGUIを開く（早すぎると接続拒否画面になる）。
        アップデート後の再起動時（VCM_NO_BROWSER=1）は開かない（既存タブが引き継ぐ）。"""
        if os.environ.get("VCM_NO_BROWSER") == "1":
            return
        while not server.started:
            if server.should_exit:  # ポート使用中などで起動に失敗した場合
                return
            await asyncio.sleep(0.1)
        gui_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        webbrowser.open(f"http://{gui_host}:{port}")

    browser_task = asyncio.create_task(open_browser_when_ready())

    async def check_update():
        info = await vcm_update.check_for_update()
        if info:
            manager.update_info = info
            print(f"[VCM] 新しいバージョン {info['version']} が公開されています: {info['url']}")
            await manager.broadcast()

    update_task = asyncio.create_task(check_update())

    print(f"[VCM] GUI: http://{host}:{port}")
    await server.serve()
    if startup:
        startup.cancel()
    update_task.cancel()
    browser_task.cancel()
    await runner.stop()
    await engine.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
