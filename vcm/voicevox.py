"""
VOICEVOX エンジンの検出・自動起動・音声合成クライアント。

読み上げ機能は VOICEVOX が使えるときだけ有効になる（機能ゲート）。
  - 既に localhost:50021 で動いていればそれを使う（勝手に止めない）
  - 未起動ならインストール先を探してエンジン (run.exe) をバックグラウンド起動
    （この場合はアプリ終了時に一緒に終了させる）
  - 見つからなければ not_installed。VCM の VC 編成機能はそのまま使える

state の遷移:
    checking       検出中
    starting       エンジンを起動して応答待ち
    ready          合成可能
    not_installed  VOICEVOX が見つからない（GUI でインストール誘導）
    error          起動失敗など
"""

import asyncio
import io
import os
import subprocess
import wave
from typing import Optional

import aiohttp

ENGINE_URL = "http://127.0.0.1:50021"

# Discord の音声送出は 48kHz / 16bit / ステレオ PCM
SAMPLE_RATE = 48000


def _engine_candidates(config_path: Optional[str]) -> list:
    """VOICEVOX エンジン実行ファイルの候補パス（優先順）。"""
    candidates = []
    if config_path:
        p = config_path.strip()
        if p:
            if os.path.isdir(p):  # フォルダ指定なら中の実行ファイルを探す
                candidates += [
                    os.path.join(p, "vv-engine", "run.exe"),
                    os.path.join(p, "run.exe"),
                    os.path.join(p, "VOICEVOX.exe"),
                ]
            else:
                candidates.append(p)
    local = os.environ.get("LOCALAPPDATA", "")
    for base in filter(None, [
        os.path.join(local, "Programs", "VOICEVOX") if local else None,
        r"C:\Program Files\VOICEVOX",
    ]):
        candidates += [
            os.path.join(base, "vv-engine", "run.exe"),  # 新しい配置
            os.path.join(base, "run.exe"),               # 旧配置
            os.path.join(base, "VOICEVOX.exe"),          # 最終手段: エディタごと起動
        ]
    return candidates


class VoicevoxEngine:
    def __init__(self, on_change=None, config_path: Optional[str] = None):
        self._on_change = on_change  # 状態変化時に GUI へ broadcast する非同期コールバック
        self._config_path = config_path
        self.state = "checking"
        self.error: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None  # 自分で起動した場合のみ保持
        self._session: Optional[aiohttp.ClientSession] = None
        self._detect_task: Optional[asyncio.Task] = None
        self._speakers_cache: Optional[list] = None

    # --- 状態管理 -------------------------------------------------------------
    async def _set_state(self, state: str, error: Optional[str] = None):
        if (state, error) == (self.state, self.error):
            return
        self.state = state
        self.error = error
        if self._on_change:
            await self._on_change()

    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60))
        return self._session

    # --- 検出・起動 -----------------------------------------------------------
    def start_detection(self):
        """検出タスクを（再）起動。GUI の「再検出」からも呼ばれる。"""
        if self._detect_task and not self._detect_task.done():
            return
        self._detect_task = asyncio.create_task(self._detect())

    async def _alive(self) -> bool:
        try:
            async with self.session().get(
                    f"{ENGINE_URL}/version",
                    timeout=aiohttp.ClientTimeout(total=2)) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _detect(self):
        await self._set_state("checking")
        if await self._alive():
            await self._set_state("ready")
            await self.warmup()
            return

        exe = next((c for c in _engine_candidates(self._config_path)
                    if os.path.isfile(c)), None)
        if exe is None:
            await self._set_state(
                "not_installed",
                "VOICEVOX が見つかりません。インストールすると読み上げ機能が使えます。")
            return

        await self._set_state("starting")
        try:
            self._proc = subprocess.Popen(
                [exe],
                cwd=os.path.dirname(exe),
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            await self._set_state("error", f"VOICEVOX の起動に失敗しました: {e}")
            return

        # エンジンの初回起動は遅いことがあるので長めに待つ
        for _ in range(120):
            await asyncio.sleep(1)
            if self._proc.poll() is not None and not await self._alive():
                await self._set_state("error", "VOICEVOX エンジンが起動直後に終了しました。")
                return
            if await self._alive():
                await self._set_state("ready")
                await self.warmup()
                return
        await self._set_state("error", "VOICEVOX エンジンの起動がタイムアウトしました。")

    async def stop(self):
        """アプリ終了時の後始末。自分で起動したエンジンだけ道連れにする。"""
        if self._detect_task and not self._detect_task.done():
            self._detect_task.cancel()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._session and not self._session.closed:
            await self._session.close()

    # --- API ------------------------------------------------------------------
    async def warmup(self, style_id: int = 3):
        """話者モデルを事前ロードして初回合成の待ち時間を減らす（失敗しても無害）。"""
        try:
            async with self.session().post(
                    f"{ENGINE_URL}/initialize_speaker",
                    params={"speaker": style_id, "skip_reinit": "true"}) as resp:
                await resp.read()
        except Exception:
            pass

    async def speakers(self, force: bool = False) -> list:
        """話者一覧 [{name, styles: [{name, id}]}]。"""
        if self._speakers_cache is not None and not force:
            return self._speakers_cache
        async with self.session().get(f"{ENGINE_URL}/speakers") as resp:
            resp.raise_for_status()
            data = await resp.json()
        self._speakers_cache = [
            {
                "name": sp["name"],
                "styles": [{"name": st["name"], "id": st["id"]} for st in sp["styles"]],
            }
            for sp in data
        ]
        return self._speakers_cache

    async def synth_pcm(self, text: str, style_id: int) -> bytes:
        """テキストを 48kHz/16bit/ステレオ の生 PCM に合成（FFmpeg 不要）。"""
        params = {"text": text, "speaker": style_id}
        async with self.session().post(f"{ENGINE_URL}/audio_query", params=params) as resp:
            resp.raise_for_status()
            query = await resp.json()
        query["outputSamplingRate"] = SAMPLE_RATE
        query["outputStereo"] = True
        async with self.session().post(
                f"{ENGINE_URL}/synthesis", params={"speaker": style_id}, json=query) as resp:
            resp.raise_for_status()
            wav_bytes = await resp.read()
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            return wf.readframes(wf.getnframes())
