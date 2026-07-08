"""
VCM の FastAPI サーバー。

  - "/"        : GUI（web/index.html）
  - "/ws"      : WebSocket。状態スナップショットを push
  - "/api/..." : サーバー選択・チーム操作・移動操作の REST

状態はサーバー（ギルド）ごとに保持する:
  - チーム編成（論理グループ。1ユーザー最大1チーム）
  - メインVC、集合直前の位置記録
  - プリセット（presets.json にギルド単位で永続化）
GUI で「操作対象サーバー」を選び、その選択に対して各操作が行われる。
"""

import asyncio
import json
import os
import random
import re
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from vcm import config as vcm_config
from vcm import tts as tts_store
from vcm import update as vcm_update

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
WEB_DIR = os.path.join(ROOT_DIR, "web")
PRESETS_PATH = os.path.join(ROOT_DIR, "presets.json")

# --- ローカルオリジンガード ------------------------------------------------------
# VCM は認証を持たないローカルツールのため、ブラウザ上の悪意ある Web ページからの
# アクセス（CSRF / WebSocket 経由の情報窃取）を Origin / Host ヘッダーで遮断する。
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _header_host(value: Optional[str]) -> Optional[str]:
    """"127.0.0.1:8765" や "[::1]:8765" からホスト部を取り出す。"""
    if not value:
        return None
    m = re.match(r"^(\[[^\]]+\]|[^:]+)", value.strip())
    return m.group(1).lower() if m else None


def origin_allowed(origin: Optional[str]) -> bool:
    """Origin ヘッダーが無い（ブラウザ以外・同一オリジンGET等）か、localhost なら許可。"""
    if not origin:
        return True
    m = re.match(r"^https?://(\[[^\]]+\]|[^/:]+)(:\d+)?$", origin.strip())
    return bool(m) and m.group(1).lower() in _LOCAL_HOSTS


def host_allowed(host_header: Optional[str]) -> bool:
    """Host ヘッダーが localhost 系か（DNSリバインディング対策）。
    config.json の host を書き換えて LAN 公開している場合はチェックしない。"""
    if vcm_config.get_host() not in ("127.0.0.1", "localhost", "::1"):
        return True  # 利用者が明示的に外部公開した構成では Host を縛れない
    h = _header_host(host_header)
    return h is None or h in _LOCAL_HOSTS


class GuildState:
    """1サーバー分の作業状態。"""

    def __init__(self):
        # teams: id -> {"id","name","member_ids":[str],"home_channel_id":str|None}
        # home_channel_id = 散開先VC（チーム見出しをVCへD&Dした最後の記録）
        self.teams: dict[int, dict] = {}
        self.next_team_id = 1
        self.main_channel_id: Optional[str] = None
        self.gathered = False  # 集合済みか（トグル表示用）
        self.pinned_ids: set[str] = set()  # シャッフルで動かさないユーザー


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()
        self.client = None  # VCMClient（BotRunner が bind / 停止時は None）
        self.runner = None  # BotRunner（main.py でセット。bot の状態を snapshot に載せる）
        self.engine = None  # VoicevoxEngine（main.py でセット。読み上げの機能ゲート）
        self.states: dict[str, GuildState] = {}  # guild_id -> GuildState
        self.selected_guild_id: Optional[str] = None
        self.default_guild_id: Optional[str] = None  # .env の GUILD_ID（初期選択）
        self.presets: dict[str, dict] = {}  # guild_id -> {name: [{"name","member_ids"}]}
        self.update_info: Optional[dict] = None  # 新バージョン情報（無ければ None）
        self.update_status = "idle"  # idle / downloading / restarting / error:...
        self.runtime_port: Optional[int] = None  # 起動時に待ち受けているポート（再起動要否判定用）
        self._lock = asyncio.Lock()
        # GUI（ブラウザ）が全て閉じたらアプリを終了するための仕組み
        self._shutdown_cb = None
        self._shutdown_task: Optional[asyncio.Task] = None
        self._load_presets()

    def bind(self, client, default_guild_id: Optional[str] = None):
        self.client = client
        self.default_guild_id = default_guild_id or None

    # --- サーバー選択 --------------------------------------------------------
    def _guild_ids(self) -> list:
        return [str(g.id) for g in self.client.guilds] if self.client else []

    def _ensure_selected(self):
        """選択中サーバーが無効なら、.env 既定 → 最初のサーバーの順で選び直す。"""
        ids = self._guild_ids()
        if self.selected_guild_id in ids:
            return
        if self.default_guild_id in ids:
            self.selected_guild_id = self.default_guild_id
        elif ids:
            self.selected_guild_id = ids[0]
        else:
            self.selected_guild_id = None

    def select_guild(self, guild_id: str):
        if guild_id not in self._guild_ids():
            raise HTTPException(status_code=404, detail="そのサーバーには接続していません")
        self.selected_guild_id = guild_id

    def state(self) -> GuildState:
        """選択中サーバーの状態（無ければ生成）。"""
        self._ensure_selected()
        gid = self.selected_guild_id
        if gid is None:
            raise HTTPException(status_code=409, detail="操作対象サーバーがありません")
        if gid not in self.states:
            self.states[gid] = GuildState()
        return self.states[gid]

    # --- プリセット永続化 ----------------------------------------------------
    def _load_presets(self):
        try:
            with open(PRESETS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        # 旧形式 {name: [...]} を検出したら後で既定サーバーへ移行するため退避
        self._legacy_presets = None
        if data and any(isinstance(v, list) for v in data.values()):
            self._legacy_presets = data
            self.presets = {}
        else:
            self.presets = data

    def migrate_legacy_presets(self):
        """旧形式（サーバー非依存）のプリセットを既定サーバーへ移行。"""
        if self._legacy_presets and self.default_guild_id:
            self.presets.setdefault(self.default_guild_id, {}).update(self._legacy_presets)
            self._legacy_presets = None
            self._save_presets()

    def _save_presets(self):
        vcm_config.save_json_atomic(PRESETS_PATH, self.presets)

    def _guild_presets(self) -> dict:
        self._ensure_selected()
        return self.presets.setdefault(self.selected_guild_id, {})

    def save_preset(self, name: str):
        st = self.state()
        self._guild_presets()[name] = [
            {"name": t["name"], "member_ids": list(t["member_ids"]),
             "home_channel_id": t.get("home_channel_id")}
            for t in st.teams.values()
        ]
        self._save_presets()

    def load_preset(self, name: str):
        preset = self._guild_presets().get(name)
        if preset is None:
            raise HTTPException(status_code=404, detail="preset not found")
        st = self.state()
        st.teams = {}
        st.next_team_id = 1
        for t in preset:
            team = self.create_team(t["name"])
            team["member_ids"] = list(t["member_ids"])
            team["home_channel_id"] = t.get("home_channel_id")  # 旧形式プリセットは None

    def delete_preset(self, name: str):
        gp = self._guild_presets()
        if name in gp:
            del gp[name]
            self._save_presets()

    # --- チーム操作（選択中サーバー） ----------------------------------------
    def create_team(self, name: str) -> dict:
        st = self.state()
        tid = st.next_team_id
        st.next_team_id += 1
        team = {"id": tid, "name": name or f"Team {tid}",
                "member_ids": [], "home_channel_id": None}
        st.teams[tid] = team
        return team

    def set_team_home(self, team_id: int, channel_id: str):
        """チームの散開先VCを記録する（チーム見出しのD&D移動時に呼ばれる）。"""
        self._require_team(team_id)["home_channel_id"] = channel_id

    def rename_team(self, team_id: int, name: str):
        self._require_team(team_id)["name"] = name

    def delete_team(self, team_id: int):
        self._require_team(team_id)
        del self.state().teams[team_id]

    def assign_member(self, team_id: int, user_id: str):
        st = self.state()
        self._require_team(team_id)
        for t in st.teams.values():  # 他チームから外す（1人1チーム）
            if user_id in t["member_ids"]:
                t["member_ids"].remove(user_id)
        st.teams[team_id]["member_ids"].append(user_id)

    def shuffle_teams(self):
        """チーム所属メンバーを、既存チームへランダムかつ均等に振り分け直す。
        ピン留めされたメンバーは現在のチームから動かさない。"""
        st = self.state()
        teams = list(st.teams.values())
        if len(teams) < 2:
            raise HTTPException(status_code=400, detail="チームが2つ以上必要です")
        members = [uid for t in teams for uid in t["member_ids"]]
        if not members:
            raise HTTPException(status_code=400, detail="チームに所属しているユーザーがいません")
        pool = [uid for uid in members if uid not in st.pinned_ids]
        if not pool:
            raise HTTPException(status_code=400, detail="全員ピン留めされているためシャッフルできません")
        random.shuffle(pool)
        random.shuffle(teams)  # 人数の端数・同数時の行き先もランダムにする
        for t in teams:
            t["member_ids"] = [uid for uid in t["member_ids"] if uid in st.pinned_ids]
        for uid in pool:  # 常に最少人数のチームへ入れて均等化（ピン留め分も人数に含む）
            min(teams, key=lambda t: len(t["member_ids"]))["member_ids"].append(uid)

    def toggle_pin(self, user_id: str) -> bool:
        """ピン留めのON/OFFを切り替え、新しい状態を返す。"""
        st = self.state()
        if user_id in st.pinned_ids:
            st.pinned_ids.discard(user_id)
            return False
        st.pinned_ids.add(user_id)
        return True

    def unassign_member(self, team_id: int, user_id: str):
        team = self._require_team(team_id)
        if user_id in team["member_ids"]:
            team["member_ids"].remove(user_id)

    def _require_team(self, team_id: int) -> dict:
        team = self.state().teams.get(team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="team not found")
        return team

    # --- 参加希望（Discord 側からのチーム選択） --------------------------------
    def teams_for_recruit(self, guild_id: str) -> list:
        """募集ボタンが押された時点のチーム一覧（GUIの選択サーバーに依存しない）。"""
        st = self.states.get(guild_id)
        if st is None:
            return []
        return [{"id": t["id"], "name": t["name"]} for t in st.teams.values()]

    async def recruit_pick(self, guild_id: str, user_id: str, team_id: int):
        """参加希望ボタン経由のチーム所属。成功したらチーム名、チーム消滅なら None。"""
        st = self.states.get(guild_id)
        team = st.teams.get(team_id) if st else None
        if team is None:
            return None
        for t in st.teams.values():  # 1人1チーム
            if user_id in t["member_ids"]:
                t["member_ids"].remove(user_id)
        team["member_ids"].append(user_id)
        await self.broadcast()
        return team["name"]

    # --- メインVC / 集合・散開（選択中サーバー） -----------------------------
    def set_main_channel(self, channel_id: Optional[str]):
        self.state().main_channel_id = channel_id or None

    async def gather(self) -> int:
        """チームに所属するユーザーをメインVCへ集める。
        チーム未所属のユーザーは対象外（無関係なVCを巻き込まない）。"""
        st = self.state()
        if not st.main_channel_id:
            raise HTTPException(status_code=400, detail="メインVCが未設定です")
        if st.gathered:
            raise HTTPException(status_code=409, detail="すでに集合済みです（散開してください）")
        snap = self.client.snapshot(self.selected_guild_id)
        location = {m["id"]: ch["id"]
                    for ch in snap.get("channels", []) for m in ch["members"]}
        targets = [
            uid
            for t in st.teams.values() for uid in t["member_ids"]
            if location.get(uid) and location[uid] != st.main_channel_id
        ]
        moved = await self.client.move_many(
            self.selected_guild_id, targets, st.main_channel_id)
        st.gathered = True
        return moved

    async def scatter(self) -> int:
        """各チームの全メンバーを、記録済みの散開先VC（home_channel_id）へ移動する。
        散開先はチーム見出しをVCへD&Dしたときに記録される。未記録のチームはスキップ。"""
        st = self.state()
        if not any(t.get("home_channel_id") for t in st.teams.values()):
            raise HTTPException(
                status_code=400,
                detail="散開先が記録されていません（チーム見出しをVCへドラッグすると記録されます）")
        moved = 0
        for t in st.teams.values():
            home = t.get("home_channel_id")
            if not home:
                continue
            moved += await self.client.move_many(
                self.selected_guild_id, list(t["member_ids"]), home)
        st.gathered = False
        return moved

    # --- snapshot / broadcast -----------------------------------------------
    def build_snapshot(self) -> dict:
        ready = bool(self.client and self.client.is_ready())
        self._ensure_selected()
        gid = self.selected_guild_id
        base = self.client.snapshot(gid) if (self.client and gid) else {"guild_name": None, "channels": []}

        index = {}
        for ch in base.get("channels", []):
            for m in ch["members"]:
                index[m["id"]] = m

        teams = []
        st = self.states.get(gid) if gid else None
        if st:
            for t in st.teams.values():
                members = [index.get(uid) or self.client.member_info(gid, uid) for uid in t["member_ids"]]
                teams.append({"id": t["id"], "name": t["name"], "members": members,
                              "home_channel_id": t.get("home_channel_id")})

        tts = {
            "engine": self.engine.state if self.engine else "off",
            "engine_error": self.engine.error if self.engine else None,
            "channel_id": None,
            "reading": None,
            "queue": [],
            "dict": tts_store.get_guild_dict(gid) if gid else {},
        }
        if ready and gid:
            tts.update(self.client.tts_state(gid))

        return {
            "ready": ready,
            "bot_state": self.runner.state if self.runner else ("ready" if ready else "connecting"),
            "bot_error": self.runner.error if self.runner else None,
            "bot_user": str(self.client.user) if (ready and self.client.user) else None,
            "tts": tts,
            "guilds": self.client.list_guilds() if self.client else [],
            "guild_id": gid,
            "guild_name": base.get("guild_name"),
            "channels": base.get("channels", []),
            "teams": teams,
            "main_channel_id": st.main_channel_id if st else None,
            "preset_names": sorted(self.presets.get(gid, {}).keys()) if gid else [],
            "can_scatter": st.gathered if st else False,
            "recruiting": bool(ready and gid and self.client.is_recruiting(gid)),
            "pinned_ids": sorted(st.pinned_ids) if st else [],
            "update": self.update_info,
            "update_status": self.update_status,
        }

    async def broadcast(self):
        if not self.active:
            return
        data = json.dumps(self.build_snapshot())
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)
        # 再接続（リロード等）があったらシャットダウン予約を取り消す
        # （完了済みタスクも None に戻す。残すと以後の終了予約が二度と入らない）
        if self._shutdown_task:
            self._shutdown_task.cancel()
            self._shutdown_task = None
        await ws.send_text(json.dumps(self.build_snapshot()))

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)
        # GUI が全て閉じたら猶予後に終了予約
        if not self.active and self._shutdown_cb and (
                self._shutdown_task is None or self._shutdown_task.done()):
            self._shutdown_task = asyncio.create_task(self._maybe_shutdown())

    def set_shutdown(self, callback):
        """全GUIが閉じたときに呼ぶ非同期コールバックを登録（main.py 側で uvicorn 停止）。"""
        self._shutdown_cb = callback

    async def _maybe_shutdown(self, grace: float = 1.0):
        try:
            await asyncio.sleep(grace)
        except asyncio.CancelledError:
            return  # 猶予中に再接続された
        if not self.active and self._shutdown_cb:
            await self._shutdown_cb()


# --- request models ----------------------------------------------------------
class SelectGuild(BaseModel):
    guild_id: str


class CreateTeam(BaseModel):
    name: Optional[str] = None


class RenameTeam(BaseModel):
    name: str


class AssignMember(BaseModel):
    user_id: str


class MoveTeam(BaseModel):
    channel_id: str


class MoveMember(BaseModel):
    user_id: str
    channel_id: str


class AssignMembers(BaseModel):
    user_ids: list[str]


class MoveMembers(BaseModel):
    user_ids: list[str]
    channel_id: str


class SetMain(BaseModel):
    channel_id: Optional[str] = None


class PinUser(BaseModel):
    user_id: str


class SavePreset(BaseModel):
    name: str


class SetToken(BaseModel):
    token: str


class Settings(BaseModel):
    guild_id: Optional[str] = None
    port: Optional[int] = None
    voicevox_path: Optional[str] = None


class TtsTest(BaseModel):
    text: Optional[str] = None


class DictEntry(BaseModel):
    word: str
    reading: str


class DictWord(BaseModel):
    word: str


def create_app(manager: ConnectionManager, runner) -> FastAPI:
    app = FastAPI(title="VCM")

    def client():
        """接続済みの VCMClient を返す。未接続なら 409。"""
        if manager.client is None or not manager.client.is_ready():
            raise HTTPException(status_code=409, detail="bot が Discord に接続していません")
        return manager.client

    @app.middleware("http")
    async def no_cache(request, call_next):
        # ローカルツールなので静的ファイルを常に最新で配信（キャッシュ起因の不整合防止）
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    @app.middleware("http")
    async def local_origin_guard(request, call_next):
        # 悪意ある Web ページからの CSRF（クロスオリジンPOST等）とDNSリバインディングを遮断
        if not origin_allowed(request.headers.get("origin")) or \
                not host_allowed(request.headers.get("host")):
            return JSONResponse(status_code=403, content={"detail": "forbidden origin"})
        return await call_next(request)

    @app.get("/")
    def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"))

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        # WebSocket は CORS の対象外のため、Origin を自前で検証してから受け入れる
        if not origin_allowed(websocket.headers.get("origin")) or \
                not host_allowed(websocket.headers.get("host")):
            await websocket.close(code=1008)  # accept 前の close はハンドシェイク拒否になる
            return
        await manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()  # クライアントからの受信は使わないが接続維持
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    # --- サーバー選択 ---
    @app.post("/api/guild")
    async def select_guild(body: SelectGuild):
        manager.select_guild(body.guild_id)
        await manager.broadcast()
        return {"ok": True}

    # --- チーム ---
    @app.post("/api/teams")
    async def create_team(body: CreateTeam):
        team = manager.create_team(body.name)
        await manager.broadcast()
        return team

    @app.post("/api/teams/shuffle")
    async def shuffle_teams():
        manager.shuffle_teams()
        await manager.broadcast()
        return {"ok": True}

    @app.post("/api/pins/toggle")
    async def toggle_pin(body: PinUser):
        pinned = manager.toggle_pin(body.user_id)
        await manager.broadcast()
        return {"pinned": pinned}

    @app.patch("/api/teams/{team_id}")
    async def rename_team(team_id: int, body: RenameTeam):
        manager.rename_team(team_id, body.name)
        await manager.broadcast()
        return {"ok": True}

    @app.delete("/api/teams/{team_id}")
    async def delete_team(team_id: int):
        manager.delete_team(team_id)
        await manager.broadcast()
        return {"ok": True}

    @app.post("/api/teams/{team_id}/members")
    async def assign_member(team_id: int, body: AssignMember):
        manager.assign_member(team_id, body.user_id)
        await manager.broadcast()
        return {"ok": True}

    @app.post("/api/teams/{team_id}/members/batch")
    async def assign_members(team_id: int, body: AssignMembers):
        for uid in body.user_ids:
            manager.assign_member(team_id, uid)
        await manager.broadcast()
        return {"ok": True}

    @app.delete("/api/teams/{team_id}/members/{user_id}")
    async def unassign_member(team_id: int, user_id: str):
        manager.unassign_member(team_id, user_id)
        await manager.broadcast()
        return {"ok": True}

    @app.post("/api/teams/{team_id}/move")
    async def move_team(team_id: int, body: MoveTeam):
        team = manager._require_team(team_id)
        moved = await client().move_many(manager.selected_guild_id, list(team["member_ids"]), body.channel_id)
        # D&Dでの移動先をチームの散開先VCとして記録（移動人数0でも意図として記録する）
        manager.set_team_home(team_id, body.channel_id)
        await manager.broadcast()
        return {"moved": moved}

    @app.post("/api/move")
    async def move_member(body: MoveMember):
        ok = await client().move_member(manager.selected_guild_id, body.user_id, body.channel_id)
        await manager.broadcast()
        return {"ok": ok}

    @app.post("/api/move/batch")
    async def move_members(body: MoveMembers):
        moved = await client().move_many(manager.selected_guild_id, body.user_ids, body.channel_id)
        await manager.broadcast()
        return {"moved": moved}

    # --- メインVC / 集合・散開 ---
    @app.post("/api/main")
    async def set_main(body: SetMain):
        changed = manager.state().main_channel_id != (body.channel_id or None)
        manager.set_main_channel(body.channel_id)
        # bot の入室はメインVCに追従（設定で入室・変更で移動・解除で退出）
        c = manager.client
        if changed and c and c.is_ready():
            # 募集メッセージは旧メインVCに紐づくため撤去（必要なら改めて募集）
            await c.recruit_stop(manager.selected_guild_id)
            if body.channel_id:
                await c.tts_join(manager.selected_guild_id, body.channel_id)
            else:
                await c.tts_leave(manager.selected_guild_id)
        await manager.broadcast()
        return {"ok": True}

    # --- 参加希望（募集） ---
    @app.post("/api/recruit/start")
    async def recruit_start():
        st = manager.state()
        gid = manager.selected_guild_id
        if not st.main_channel_id:
            raise HTTPException(status_code=400, detail="メインVCが未設定です")
        if not st.teams:
            raise HTTPException(status_code=400, detail="チームを1つ以上作成してください")
        ok = await client().recruit_start(
            gid, st.main_channel_id,
            get_teams=lambda: manager.teams_for_recruit(gid),
            on_pick=manager.recruit_pick,
        )
        if not ok:
            raise HTTPException(
                status_code=500,
                detail="募集メッセージを送信できませんでした（bot にメインVCでの発言権限があるか確認してください）")
        await manager.broadcast()
        return {"ok": True}

    @app.post("/api/recruit/stop")
    async def recruit_stop():
        await client().recruit_stop(manager.selected_guild_id)
        await manager.broadcast()
        return {"ok": True}

    @app.post("/api/gather")
    async def gather():
        client()  # bot 未接続なら 409
        moved = await manager.gather()
        await manager.broadcast()
        return {"moved": moved}

    @app.post("/api/scatter")
    async def scatter():
        client()  # bot 未接続なら 409
        moved = await manager.scatter()
        await manager.broadcast()
        return {"moved": moved}

    # --- 自動アップデート ---
    @app.post("/api/update/apply")
    async def update_apply():
        info = manager.update_info
        if not info:
            raise HTTPException(status_code=409, detail="適用できる更新がありません")
        if not info.get("can_apply"):
            raise HTTPException(
                status_code=400,
                detail="この環境では自動アップデートできません。リリースページから手動で更新してください")
        if manager.update_status in ("downloading", "restarting"):
            raise HTTPException(status_code=409, detail="すでにアップデート処理中です")
        manager.update_status = "downloading"
        await manager.broadcast()
        try:
            await vcm_update.prepare_and_launch(info)
        except Exception as e:
            manager.update_status = f"error:{e}"
            await manager.broadcast()
            raise HTTPException(status_code=500, detail=f"アップデートに失敗しました: {e}")
        manager.update_status = "restarting"
        await manager.broadcast()

        async def shutdown_later():
            await asyncio.sleep(1.0)  # broadcast とレスポンスを届けてから終了
            if manager._shutdown_cb:
                await manager._shutdown_cb()

        asyncio.create_task(shutdown_later())
        return {"ok": True}

    # --- 設定（GUI から編集） ---
    @app.get("/api/settings")
    def get_settings():
        return {**vcm_config.settings_info(), "running_port": manager.runtime_port}

    @app.post("/api/settings")
    async def update_settings(body: Settings):
        if body.port is not None and not (1 <= body.port <= 65535):
            raise HTTPException(status_code=400, detail="ポート番号は 1〜65535 で指定してください")
        old_vv = vcm_config.get_voicevox_path()
        vcm_config.save_settings(
            guild_id=body.guild_id, port=body.port, voicevox_path=body.voicevox_path)
        # VOICEVOX パスが変わったら即座に再検出（再起動不要）
        new_vv = vcm_config.get_voicevox_path()
        if new_vv != old_vv and manager.engine:
            manager.engine.set_config_path(new_vv)
            manager.engine.start_detection()
        # ポート変更は再起動しないと反映されない
        restart_required = bool(
            body.port is not None and manager.runtime_port
            and body.port != manager.runtime_port)
        await manager.broadcast()
        return {"ok": True, "restart_required": restart_required}

    # --- トークン管理 ---
    @app.get("/api/token")
    def token_info():
        return vcm_config.token_info()

    @app.post("/api/token")
    async def set_token(body: SetToken):
        token = body.token.strip()
        if not token:
            raise HTTPException(status_code=400, detail="トークンが空です")
        ok = await runner.start(token)
        if ok:
            vcm_config.save_token(token)  # ログイン成功を確認してから保存
        return {"ok": ok, "error": runner.error}

    @app.delete("/api/token")
    async def delete_token():
        vcm_config.delete_token()
        await runner.clear()
        return {"ok": True}

    # --- 読み上げ ---
    def _engine_ready():
        if manager.engine is None or manager.engine.state != "ready":
            raise HTTPException(status_code=409, detail="VOICEVOX が起動していません")

    @app.post("/api/tts/skip")
    async def tts_skip():
        reader = client().get_reader(manager.selected_guild_id)
        if reader:
            reader.skip()
        return {"ok": True}

    @app.post("/api/tts/clear")
    async def tts_clear():
        reader = client().get_reader(manager.selected_guild_id)
        if reader:
            reader.clear()
            reader.skip()
        await manager.broadcast()
        return {"ok": True}

    @app.post("/api/tts/test")
    async def tts_test(body: TtsTest):
        _engine_ready()
        reader = client().get_reader(manager.selected_guild_id)
        if reader is None:
            raise HTTPException(status_code=409, detail="VC に入室していません")
        reader.enqueue(body.text or "読み上げのテストです。", tts_store.DEFAULT_STYLE_ID)
        await manager.broadcast()
        return {"ok": True}

    @app.post("/api/tts/redetect")
    async def tts_redetect():
        if manager.engine:
            manager.engine.start_detection()
        return {"ok": True}

    # --- 読み上げ辞書 ---
    @app.post("/api/tts/dict")
    async def dict_add(body: DictEntry):
        word = body.word.strip()
        reading = body.reading.strip()
        if not word or not reading:
            raise HTTPException(status_code=400, detail="単語と読みを入力してください")
        manager._ensure_selected()
        if manager.selected_guild_id is None:
            raise HTTPException(status_code=409, detail="操作対象サーバーがありません")
        tts_store.set_dict_entry(manager.selected_guild_id, word, reading)
        await manager.broadcast()
        return {"ok": True}

    @app.post("/api/tts/dict/delete")
    async def dict_delete(body: DictWord):
        manager._ensure_selected()
        if manager.selected_guild_id is None:
            raise HTTPException(status_code=409, detail="操作対象サーバーがありません")
        tts_store.delete_dict_entry(manager.selected_guild_id, body.word)
        await manager.broadcast()
        return {"ok": True}

    # --- プリセット ---
    @app.post("/api/presets")
    async def save_preset(body: SavePreset):
        manager.save_preset(body.name)
        await manager.broadcast()
        return {"ok": True}

    @app.post("/api/presets/{name}/load")
    async def load_preset(name: str):
        manager.load_preset(name)
        await manager.broadcast()
        return {"ok": True}

    @app.delete("/api/presets/{name}")
    async def delete_preset(name: str):
        manager.delete_preset(name)
        await manager.broadcast()
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
