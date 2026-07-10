"""
チュートリアル用のデモモード。

DemoClient は VCMClient と同じインターフェース（の必要な部分）を持つ偽クライアント。
偽のサーバー・VC・メンバーをメモリ上に持ち、移動系の操作で実際に状態が変わる。
ConnectionManager.client を一時的にこれへ差し替えることで、本物の API・
サーバーロジック（シャッフル・集合散開・ピン留め等）がそのままデモデータに対して動く。

Discord へ送信する系の操作（読み上げ・募集・コマンド）は無害な no-op。
"""

from typing import Optional
from urllib.parse import quote

DEMO_GUILD_ID = "demo"

_PALETTE = ["#5865F2", "#57F287", "#FEE75C", "#EB459E", "#ED4245",
            "#3BA55D", "#FAA61A", "#00B0F4", "#9B59B6", "#E67E22"]


def _avatar(i: int, initial: str) -> str:
    """イニシャル入りの簡易アバター（data URI の SVG）。"""
    c = _PALETTE[i % len(_PALETTE)]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
           f'<rect width="64" height="64" fill="{c}"/>'
           f'<text x="32" y="42" font-size="30" font-family="sans-serif" '
           f'font-weight="bold" fill="#fff" text-anchor="middle">{initial}</text></svg>')
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


_MEMBER_NAMES = ["そら", "Kaito", "ユウキ", "あおい", "Rin",
                 "たくみ", "ミナト", "Haru", "ひなた", "さくら"]


class DemoClient:
    """デモ用の偽 VCMClient。VC 状態をメモリで持ち、移動で実際に動く。"""

    def __init__(self):
        # channel_id -> {"name", "category", "member_ids": [str]}
        self.channels = {
            "demo-lobby": {"name": "集合ロビー", "category": "🎓 チュートリアル",
                           "member_ids": [str(i + 1) for i in range(len(_MEMBER_NAMES))]},
            "demo-vc1": {"name": "対戦VC 1", "category": "🎓 チュートリアル", "member_ids": []},
            "demo-vc2": {"name": "対戦VC 2", "category": "🎓 チュートリアル", "member_ids": []},
        }
        self.members = {
            str(i + 1): {"id": str(i + 1), "name": name, "avatar": _avatar(i, name[0].upper())}
            for i, name in enumerate(_MEMBER_NAMES)
        }
        self.joined_channel_id: Optional[str] = None  # 読み上げbotの入室VC（見た目のみ）
        self.user = "チュートリアル（デモ）"

    # --- VCMClient 互換インターフェース ----------------------------------------
    def is_ready(self) -> bool:
        return True

    def is_closed(self) -> bool:
        return False

    @property
    def guilds(self):
        class _G:
            id = DEMO_GUILD_ID
        return [_G()]

    def list_guilds(self) -> list:
        return [{"id": DEMO_GUILD_ID, "name": "デモサーバー", "icon": ""}]

    def snapshot(self, guild_id) -> dict:
        if guild_id != DEMO_GUILD_ID:
            return {"guild_name": None, "channels": []}
        channels = []
        for cid, ch in self.channels.items():
            channels.append({
                "id": cid,
                "name": ch["name"],
                "category": ch["category"],
                "category_id": "demo-cat",
                "members": [self._member_dict(uid) for uid in ch["member_ids"]],
            })
        return {"guild_name": "デモサーバー", "channels": channels}

    def _member_dict(self, uid: str) -> dict:
        m = self.members[uid]
        return {"id": uid, "name": m["name"], "avatar": m["avatar"],
                "voice_channel_id": self._where(uid)}

    def _where(self, uid: str) -> Optional[str]:
        for cid, ch in self.channels.items():
            if uid in ch["member_ids"]:
                return cid
        return None

    def member_info(self, guild_id, user_id) -> dict:
        if user_id in self.members:
            return self._member_dict(user_id)
        return {"id": user_id, "name": f"({user_id})", "avatar": "", "voice_channel_id": None}

    async def move_member(self, guild_id, user_id, channel_id) -> bool:
        cur = self._where(str(user_id))
        if cur is None or channel_id not in self.channels or cur == channel_id:
            return False
        self.channels[cur]["member_ids"].remove(str(user_id))
        self.channels[channel_id]["member_ids"].append(str(user_id))
        return True

    async def move_many(self, guild_id, user_ids, channel_id) -> int:
        moved = 0
        for uid in user_ids:
            if await self.move_member(guild_id, uid, channel_id):
                moved += 1
        return moved

    # --- 読み上げ・募集は見た目だけ / no-op --------------------------------------
    def tts_state(self, guild_id) -> dict:
        return {"channel_id": self.joined_channel_id, "reading": None, "queue": []}

    def get_reader(self, guild_id):
        return None

    async def tts_join(self, guild_id, channel_id) -> bool:
        self.joined_channel_id = channel_id
        return True

    async def tts_leave(self, guild_id):
        self.joined_channel_id = None

    def is_recruiting(self, guild_id) -> bool:
        return False

    async def recruit_start(self, *args, **kwargs) -> bool:
        return False  # デモでは募集メッセージを送れない（GUI 側で案内）

    async def recruit_stop(self, guild_id):
        pass
