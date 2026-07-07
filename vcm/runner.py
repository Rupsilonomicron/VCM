"""
Discord クライアント (VCMClient) のライフサイクル管理。

トークン未設定でも Web サーバー（GUI）は先に起動しておき、
GUI から入力されたトークンで bot を起動・再起動できるようにする。

state の遷移:
    no_token      トークン未設定（bot 停止中）
    connecting    ログイン成功、ゲートウェイ接続中
    ready         接続完了（操作可能）
    invalid_token トークンが不正でログイン失敗
    error         その他の失敗（インテント未有効化など）
"""

import asyncio
from typing import Optional

import discord

from vcm.discord_bot import VCMClient


class BotRunner:
    def __init__(self, manager, default_guild_id: Optional[str] = None, engine=None):
        self.manager = manager
        self.default_guild_id = default_guild_id
        self.engine = engine  # VoicevoxEngine（読み上げ機能。None なら無効）
        self.client: Optional[VCMClient] = None
        self._connect_task: Optional[asyncio.Task] = None
        self.state = "no_token"
        self.error: Optional[str] = None

    async def _on_change(self):
        if self.state == "connecting" and self.client and self.client.is_ready():
            self.state = "ready"
        await self.manager.broadcast()

    async def start(self, token: str) -> bool:
        """トークンを検証して bot を起動。ログイン成功なら True。

        検証はログイン試行そのもので行う（無効なら invalid_token になる）。
        """
        await self.stop()
        self.state = "connecting"
        self.error = None
        client = VCMClient(on_change=self._on_change, engine=self.engine)
        try:
            await client.login(token)
        except discord.LoginFailure:
            await client.close()
            self.state = "invalid_token"
            self.error = "トークンが無効です。コピーミスがないか、トークンを再生成していないか確認してください。"
            await self.manager.broadcast()
            return False
        except Exception as e:
            await client.close()
            self.state = "error"
            self.error = f"Discord への接続に失敗しました: {e}"
            await self.manager.broadcast()
            return False

        self.client = client
        self.manager.bind(client, default_guild_id=self.default_guild_id)
        self._connect_task = asyncio.create_task(self._run(client))
        await self.manager.broadcast()
        return True

    async def _run(self, client: VCMClient):
        try:
            await client.connect()
        except asyncio.CancelledError:
            raise
        except discord.PrivilegedIntentsRequired:
            self.state = "error"
            self.error = (
                "特権インテントが無効です。Discord Developer Portal の Bot ページで "
                "Privileged Gateway Intents の SERVER MEMBERS INTENT と "
                "MESSAGE CONTENT INTENT を両方 ON にしてください。"
            )
            await client.close()
            await self.manager.broadcast()
        except Exception as e:
            self.state = "error"
            self.error = f"Discord 接続中にエラーが発生しました: {e}"
            await client.close()
            await self.manager.broadcast()

    async def stop(self):
        """bot を停止して切断。GUI（Web サーバー）は動き続ける。"""
        client, self.client = self.client, None
        task, self._connect_task = self._connect_task, None
        if client and not client.is_closed():
            await client.close()
        if task:
            try:
                await task
            except Exception:
                pass
        if self.manager.client is client:
            self.manager.client = None

    async def clear(self):
        """トークン削除時: bot を止めて未設定状態に戻す。"""
        await self.stop()
        self.state = "no_token"
        self.error = None
        await self.manager.broadcast()
