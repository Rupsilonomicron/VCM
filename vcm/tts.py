"""
読み上げ制御。

  - tts.json への永続化（ユーザーごとの声設定・サーバーごとの辞書）
  - テキスト前処理（辞書適用・URL/絵文字/コードブロックの整形・長文カット）
  - GuildReader: サーバー1つ分の読み上げキューと再生ループ

GuildReader は VCMClient（discord_bot.py）が VC 入室時に生成し、退出時に破棄する。
"""

import asyncio
import io
import json
import os
import re
from typing import Optional

import discord

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
TTS_STORE_PATH = os.path.join(ROOT_DIR, "tts.json")

DEFAULT_STYLE_ID = 3  # ずんだもん（ノーマル）
MAX_TEXT_LEN = 120
QUEUE_LIMIT = 50  # 荒らし・連投対策の上限。超えた分は捨てる

_URL_RE = re.compile(r"https?://\S+")
_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
_CODEBLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_SPACES_RE = re.compile(r"\s+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")


# --- 永続化（tts.json） --------------------------------------------------------
def _load_store() -> dict:
    try:
        with open(TTS_STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_store(store: dict):
    with open(TTS_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def get_user_voice(user_id) -> int:
    voices = _load_store().get("user_voices", {})
    try:
        return int(voices.get(str(user_id), DEFAULT_STYLE_ID))
    except (TypeError, ValueError):
        return DEFAULT_STYLE_ID


def set_user_voice(user_id, style_id: int):
    store = _load_store()
    store.setdefault("user_voices", {})[str(user_id)] = int(style_id)
    _save_store(store)


def get_guild_dict(guild_id) -> dict:
    guilds = _load_store().get("guilds", {})
    d = guilds.get(str(guild_id), {}).get("dict", {})
    return d if isinstance(d, dict) else {}


def set_dict_entry(guild_id, word: str, reading: str):
    store = _load_store()
    g = store.setdefault("guilds", {}).setdefault(str(guild_id), {})
    g.setdefault("dict", {})[word] = reading
    _save_store(store)


def delete_dict_entry(guild_id, word: str):
    store = _load_store()
    d = store.get("guilds", {}).get(str(guild_id), {}).get("dict", {})
    if word in d:
        del d[word]
        _save_store(store)


# --- テキスト前処理 -------------------------------------------------------------
def preprocess(text: str, guild_dict: dict) -> str:
    t = _CODEBLOCK_RE.sub(" コード省略 ", text)
    t = _EMOJI_RE.sub(r"\1", t)         # カスタム絵文字は名前だけ読む
    t = _URL_RE.sub(" URL ", t)
    # 辞書は長い単語から順に適用（部分一致の食い合いを防ぐ）
    for word in sorted(guild_dict, key=len, reverse=True):
        reading = guild_dict[word]
        if _ASCII_WORD_RE.fullmatch(word):
            # 英数字のみの単語は前後が英数字でない場合だけ置換
            # （「w」の登録が Windows などの一部に反応しないように）。
            # 1文字の登録語は「wwww」のような連続を1回の読みにまとめる。
            body = re.escape(word) + ("+" if len(word) == 1 else "")
            t = re.sub(
                rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])",
                lambda _: reading,
                t,
            )
        else:
            t = t.replace(word, reading)
    t = _SPACES_RE.sub(" ", t).strip()
    if len(t) > MAX_TEXT_LEN:
        t = t[:MAX_TEXT_LEN] + " 以下省略"
    return t


# --- 読み上げループ -------------------------------------------------------------
class GuildReader:
    """サーバー1つ分の読み上げキュー。VC 入室中だけ存在する。"""

    def __init__(self, client, engine, guild_id: int, channel_id: int):
        self.client = client
        self.engine = engine
        self.guild_id = guild_id
        self.channel_id = channel_id  # 入室中の VC（＝読み上げ対象の内蔵チャット）
        self.queue: asyncio.Queue = asyncio.Queue()
        self.current: Optional[str] = None
        self._play_task: Optional[asyncio.Task] = None
        self._skipping = False
        self._task = asyncio.create_task(self._loop())

    def enqueue(self, text: str, style_id: int):
        if self.queue.qsize() >= QUEUE_LIMIT:
            return
        self.queue.put_nowait((text, style_id))

    def snapshot(self) -> dict:
        return {
            "channel_id": str(self.channel_id),
            "reading": self.current,
            "queue": [t for t, _ in list(self.queue._queue)],  # 表示用
        }

    def skip(self):
        """処理中の1件を打ち切る（合成待ちの間でも即座に次へ進める）。"""
        task = self._play_task
        if task and not task.done():
            self._skipping = True
            task.cancel()
        guild = self.client.get_guild(self.guild_id)
        if guild and guild.voice_client:
            guild.voice_client.stop()

    def clear(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def close(self):
        if self._play_task and not self._play_task.done():
            self._play_task.cancel()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self.clear()
        self.current = None

    async def _loop(self):
        while True:
            text, style_id = await self.queue.get()
            self.current = text
            await self._notify()
            self._play_task = asyncio.create_task(self._play(text, style_id))
            try:
                await self._play_task
            except asyncio.CancelledError:
                if not self._skipping:
                    raise  # close() によるキャンセルは伝播させてループを終了する
            except Exception as e:
                print(f"[VCM] TTS 再生エラー: {e}")
            finally:
                self._skipping = False
                self._play_task = None
            self.current = None
            await self._notify()

    async def _play(self, text: str, style_id: int):
        guild = self.client.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        if vc is None or not vc.is_connected():
            return
        pcm = await self.engine.synth_pcm(text, style_id)
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def after(_err):
            loop.call_soon_threadsafe(done.set)

        vc.play(discord.PCMAudio(io.BytesIO(pcm)), after=after)
        await done.wait()

    async def _notify(self):
        try:
            await self.client._on_change()
        except Exception:
            pass
