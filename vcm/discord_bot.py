"""
VCM (VoiceChatMover) の Discord クライアント。

役割:
  - bot が参加している全サーバー（ギルド）のVC・接続ユーザーを取得
  - voice state / ギルド参加退出の変化を on_change で通知（→ WebSocket ブロードキャスト）
  - 指定サーバー内でのユーザー VC 移動（個人／一括）を実行
  - 読み上げ（VOICEVOX）: メインVC の設定に追従して入室し、入室中VCの内蔵チャットを読み上げ、
    /voice スラッシュコマンドでユーザーごとの声設定

「どのサーバーを操作対象にするか」は server.py 側（ConnectionManager）が保持する。
このクラスは guild_id を引数で受け取り、対象サーバーに対して処理する。
"""

import asyncio
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands

from vcm import tts


class VCMClient(discord.Client):
    def __init__(self, on_change: Callable[[], Awaitable[None]], engine=None):
        intents = discord.Intents.default()
        intents.members = True          # 特権インテント（開発者ポータルで要有効化）
        intents.voice_states = True
        intents.guilds = True
        intents.message_content = True  # 特権インテント（読み上げ用・要有効化）
        super().__init__(intents=intents)
        self._on_change = on_change
        self.engine = engine  # VoicevoxEngine（None なら読み上げ無効）
        self.readers: dict[int, tts.GuildReader] = {}  # guild_id -> GuildReader
        self.recruits: dict[int, dict] = {}  # guild_id -> {"message","view"}（参加希望の募集）
        self.tree = app_commands.CommandTree(self)
        self._register_commands()
        self._synced_guilds: set[int] = set()

    async def close(self):
        for rec in list(self.recruits.values()):  # 募集メッセージを残さない
            rec["view"].stop()
            try:
                await rec["message"].delete()
            except discord.HTTPException:
                pass
        self.recruits.clear()
        for reader in list(self.readers.values()):
            await reader.close()
        self.readers.clear()
        await super().close()

    # --- events ---------------------------------------------------------------
    async def on_ready(self):
        print(f"[VCM] logged in as {self.user} ({len(self.guilds)} servers)")
        for guild in self.guilds:
            await self._sync_commands(guild)
        await self._on_change()

    async def on_voice_state_update(self, member, before, after):
        await self._on_change()

    async def on_guild_join(self, guild):
        await self._sync_commands(guild)
        await self._on_change()

    async def on_guild_remove(self, guild):
        reader = self.readers.pop(guild.id, None)
        if reader:
            await reader.close()
        rec = self.recruits.pop(guild.id, None)
        if rec:
            rec["view"].stop()
        await self._on_change()

    async def on_message(self, message: discord.Message):
        """入室中VCの内蔵チャットだけを読み上げ対象にする。"""
        if message.author.bot or message.guild is None:
            return
        reader = self.readers.get(message.guild.id)
        if reader is None or message.channel.id != reader.channel_id:
            return
        text = tts.preprocess(message.clean_content, tts.get_guild_dict(message.guild.id))
        if not text:
            return
        reader.enqueue(text, tts.get_user_voice(message.author.id))
        await self._on_change()

    async def _sync_commands(self, guild: discord.Guild):
        """スラッシュコマンドをギルド単位で同期（即時反映のため）。"""
        if guild.id in self._synced_guilds:
            return
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self._synced_guilds.add(guild.id)
        except discord.HTTPException as e:
            print(f"[VCM] command sync failed for {guild.name}: {e}")

    # --- guild 一覧 / 参照 ----------------------------------------------------
    def list_guilds(self) -> list:
        return [
            {
                "id": str(g.id),
                "name": g.name,
                "icon": g.icon.url if g.icon else "",
            }
            for g in self.guilds
        ]

    def get_guild_by_id(self, guild_id: Optional[str]) -> Optional[discord.Guild]:
        return self.get_guild(int(guild_id)) if guild_id else None

    # --- member ファクト ------------------------------------------------------
    def _member_dict(self, member: discord.Member) -> dict:
        ch = member.voice.channel if member.voice else None
        return {
            "id": str(member.id),
            "name": member.display_name,
            "avatar": member.display_avatar.url,
            "voice_channel_id": str(ch.id) if ch else None,
        }

    def member_info(self, guild_id: str, user_id: str) -> dict:
        """VC にいない場合でも名前・アイコンを引けるようにするための補助。"""
        guild = self.get_guild_by_id(guild_id)
        member = guild.get_member(int(user_id)) if guild else None
        if member:
            return self._member_dict(member)
        return {"id": user_id, "name": f"({user_id})", "avatar": "", "voice_channel_id": None}

    # --- snapshot ------------------------------------------------------------
    def snapshot(self, guild_id: Optional[str]) -> dict:
        guild = self.get_guild_by_id(guild_id)
        if guild is None:
            return {"guild_name": None, "channels": []}
        channels = []
        # by_category() は Discord クライアントの表示順（カテゴリ順・カテゴリ内の位置順）
        for category, chs in guild.by_category():
            for vc in chs:
                if not isinstance(vc, discord.VoiceChannel):
                    continue
                channels.append({
                    "id": str(vc.id),
                    "name": vc.name,
                    "category": category.name if category else None,
                    "category_id": str(category.id) if category else None,
                    "members": [self._member_dict(m) for m in vc.members if not m.bot],
                })
        return {"guild_name": guild.name, "channels": channels}

    # --- 移動操作 ------------------------------------------------------------
    async def move_member(self, guild_id: str, user_id: str, channel_id: str) -> bool:
        """user を channel へ移動。user が VC に未接続なら何もしない（Discord の制約）。"""
        guild = self.get_guild_by_id(guild_id)
        if guild is None:
            return False
        member = guild.get_member(int(user_id))
        channel = guild.get_channel(int(channel_id))
        if member and member.voice and channel and isinstance(channel, discord.VoiceChannel):
            try:
                await member.move_to(channel)
                return True
            except discord.HTTPException as e:
                print(f"[VCM] move failed user={user_id} ch={channel_id}: {e}")
        return False

    async def move_many(self, guild_id: str, user_ids, channel_id: str) -> int:
        """複数ユーザーを順次移動。discord.py が内部でレート制限を吸収する。"""
        moved = 0
        for uid in user_ids:
            if await self.move_member(guild_id, uid, channel_id):
                moved += 1
        return moved

    # --- 読み上げ（VC 入退室・キュー操作） -------------------------------------
    def get_reader(self, guild_id: str) -> Optional[tts.GuildReader]:
        try:
            return self.readers.get(int(guild_id))
        except (TypeError, ValueError):
            return None

    def tts_state(self, guild_id: Optional[str]) -> dict:
        reader = self.get_reader(guild_id) if guild_id else None
        if reader is None:
            return {"channel_id": None, "reading": None, "queue": []}
        return reader.snapshot()

    async def tts_join(self, guild_id: str, channel_id: str) -> bool:
        """VC に入室（既に別VCにいれば移動）し、そのVCの内蔵チャットを読み上げ対象にする。"""
        guild = self.get_guild_by_id(guild_id)
        channel = guild.get_channel(int(channel_id)) if guild else None
        if guild is None or not isinstance(channel, discord.VoiceChannel):
            return False
        if guild.voice_client and guild.voice_client.is_connected():
            await guild.voice_client.move_to(channel)
        else:
            await channel.connect()
        reader = self.readers.get(guild.id)
        if reader is None:
            self.readers[guild.id] = tts.GuildReader(self, self.engine, guild.id, channel.id)
        else:
            reader.channel_id = channel.id  # 読み上げ対象を移動先VCに切替
            reader.clear()
        await self._on_change()
        return True

    async def tts_leave(self, guild_id: str):
        guild = self.get_guild_by_id(guild_id)
        if guild is None:
            return
        reader = self.readers.pop(guild.id, None)
        if reader:
            await reader.close()
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)
        await self._on_change()

    # --- 参加希望（募集） ------------------------------------------------------
    def is_recruiting(self, guild_id: Optional[str]) -> bool:
        try:
            return int(guild_id) in self.recruits
        except (TypeError, ValueError):
            return False

    async def recruit_start(self, guild_id: str, channel_id: str,
                            get_teams: Callable[[], list],
                            on_pick: Callable[[str, str, int], Awaitable[Optional[str]]]) -> bool:
        """メインVCの内蔵チャットに「参加希望」ボタン付きの募集メッセージを設置する。

        get_teams: 押された時点のチーム一覧 [{"id","name"}] を返す（server.py の状態を参照）
        on_pick:   (guild_id, user_id, team_id) -> チーム名（所属成功）/ None（チーム消滅）
        """
        guild = self.get_guild_by_id(guild_id)
        channel = guild.get_channel(int(channel_id)) if guild else None
        if not isinstance(channel, discord.VoiceChannel):
            return False
        await self.recruit_stop(guild_id)  # 既に募集中なら張り替え
        view = RecruitView(channel.id, get_teams, on_pick)
        try:
            message = await channel.send(
                "✋ **参加希望 受付中**\n"
                f"{channel.mention} に接続した状態で下のボタンを押すと、参加するチームを選べます。",
                view=view,
            )
        except discord.HTTPException as e:
            print(f"[VCM] recruit message failed ch={channel_id}: {e}")
            return False
        self.recruits[guild.id] = {"message": message, "view": view}
        return True

    async def recruit_stop(self, guild_id: str):
        guild = self.get_guild_by_id(guild_id)
        if guild is None:
            return
        rec = self.recruits.pop(guild.id, None)
        if rec:
            rec["view"].stop()
            try:
                await rec["message"].delete()
            except discord.HTTPException:
                pass  # 手動削除済みなどは無視

    # --- /voice スラッシュコマンド ---------------------------------------------
    def _register_commands(self):
        @self.tree.command(name="voice", description="読み上げの声を設定します")
        async def voice_cmd(interaction: discord.Interaction):
            if self.engine is None or self.engine.state != "ready":
                await interaction.response.send_message(
                    "VOICEVOX が起動していないため、いまは声を変更できません。", ephemeral=True)
                return
            try:
                speakers = await self.engine.speakers()
            except Exception:
                await interaction.response.send_message(
                    "話者一覧の取得に失敗しました。", ephemeral=True)
                return
            await interaction.response.send_message(
                PICKER_PROMPT,
                embed=picker_embed(speakers),
                view=CharacterPickerView(speakers),
                ephemeral=True,
            )


# --- 参加希望の選択UI ------------------------------------------------------------
RECRUIT_PICK_TIMEOUT = 60  # チーム選択メッセージを放置したとき自動で消えるまでの秒数


class RecruitView(discord.ui.View):
    """募集メッセージに付ける「参加希望」ボタン（募集を締め切るまで有効）。"""

    def __init__(self, channel_id: int, get_teams, on_pick):
        super().__init__(timeout=None)
        self.channel_id = channel_id  # メインVC（このVCの接続者だけが使える）
        self.get_teams = get_teams
        self.on_pick = on_pick

    @discord.ui.button(label="✋ 参加希望", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None or voice.channel.id != self.channel_id:
            await interaction.response.send_message(
                f"このボタンは <#{self.channel_id}> に接続している人だけが使えます。"
                "VCに参加してから押してください。",
                ephemeral=True, delete_after=10)
            return
        teams = self.get_teams()
        if not teams:
            await interaction.response.send_message(
                "いま選べるチームがありません。チームが作られるまで待ってください。",
                ephemeral=True, delete_after=10)
            return
        await interaction.response.send_message(
            f"参加するチームを選んでください（{RECRUIT_PICK_TIMEOUT}秒たつと自動で閉じます）:",
            view=TeamPickView(interaction, teams, self.on_pick),
            ephemeral=True)


class TeamPickView(discord.ui.View):
    """参加希望者ごとのチーム選択（本人のみ閲覧可）。選択または時間切れで消える。"""

    def __init__(self, origin: discord.Interaction, teams: list, on_pick):
        super().__init__(timeout=RECRUIT_PICK_TIMEOUT)
        self.origin = origin  # 元の ephemeral メッセージの削除に使う
        self.on_pick = on_pick
        for t in teams[:25]:  # ボタン上限（5行×5個）
            btn = discord.ui.Button(label=str(t["name"])[:80], style=discord.ButtonStyle.secondary)
            btn.callback = self._make_pick(t["id"])
            self.add_item(btn)

    def _make_pick(self, team_id: int):
        async def pick(interaction: discord.Interaction):
            self.stop()  # 以降は時間切れ削除を走らせない
            name = await self.on_pick(str(interaction.guild_id), str(interaction.user.id), team_id)
            if name is None:
                await interaction.response.edit_message(
                    content="そのチームは削除されていました。もう一度「参加希望」を押してください。",
                    view=None)
                await self._delete_later()
                return
            await interaction.response.edit_message(
                content=f"✅ **{name}** に参加しました！", view=None)
            await self._delete_later()
        return pick

    async def _delete_later(self, delay: float = 5.0):
        """結果を一瞬見せてからメッセージを消す。"""
        async def run():
            await asyncio.sleep(delay)
            try:
                await self.origin.delete_original_response()
            except discord.HTTPException:
                pass
        asyncio.create_task(run())

    async def on_timeout(self):
        try:
            await self.origin.delete_original_response()
        except discord.HTTPException:
            pass


# --- /voice の選択UI ------------------------------------------------------------
PAGE_SIZE = 20
PICKER_PROMPT = "読み上げに使う声（キャラクター）を選んでください:"


def picker_embed(speakers: list) -> Optional[discord.Embed]:
    """どのページにどのキャラがいるかの一覧（1ページに収まるときは不要なので None）。"""
    pages = max(1, -(-len(speakers) // PAGE_SIZE))
    if pages <= 1:
        return None
    embed = discord.Embed()
    for p in range(pages):
        chunk = speakers[p * PAGE_SIZE:(p + 1) * PAGE_SIZE]
        embed.add_field(
            name=f"{p + 1}ページ",
            value="\n".join(sp["name"] for sp in chunk)[:1024],  # フィールド上限
            inline=True,  # 横に並べて列として表示
        )
    return embed


class CharacterPickerView(discord.ui.View):
    """キャラクター選択（◀▶でページ送り）→ スタイル選択の2段階。"""

    def __init__(self, speakers: list, page: int = 0):
        super().__init__(timeout=300)
        self.speakers = speakers
        self.page = page
        self._build()

    def _build(self):
        self.clear_items()
        pages = max(1, -(-len(self.speakers) // PAGE_SIZE))
        self.page %= pages
        chunk = self.speakers[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]

        select = discord.ui.Select(
            placeholder=f"キャラクター（{self.page + 1}/{pages} ページ）",
            options=[
                discord.SelectOption(label=sp["name"], value=str(self.page * PAGE_SIZE + i))
                for i, sp in enumerate(chunk)
            ],
        )

        async def on_select(interaction: discord.Interaction):
            sp = self.speakers[int(select.values[0])]
            if len(sp["styles"]) == 1:  # スタイルが1つだけなら即決定
                st = sp["styles"][0]
                tts.set_user_voice(interaction.user.id, st["id"])
                await interaction.response.edit_message(
                    content=f"✅ 声を **{sp['name']}（{st['name']}）** に設定しました。",
                    embed=None, view=None)
                return
            await interaction.response.edit_message(
                content=f"**{sp['name']}** のスタイルを選んでください:",
                embed=None,
                view=StylePickerView(self.speakers, sp),
            )

        select.callback = on_select
        self.add_item(select)

        if pages > 1:
            prev_btn = discord.ui.Button(label="◀", row=1)
            next_btn = discord.ui.Button(label="▶", row=1)

            async def go(interaction: discord.Interaction, delta: int):
                self.page += delta
                self._build()
                await interaction.response.edit_message(view=self)

            prev_btn.callback = lambda i: go(i, -1)
            next_btn.callback = lambda i: go(i, +1)
            self.add_item(prev_btn)
            self.add_item(next_btn)


class StylePickerView(discord.ui.View):
    def __init__(self, speakers: list, speaker: dict):
        super().__init__(timeout=300)
        select = discord.ui.Select(
            placeholder="スタイル",
            options=[
                discord.SelectOption(label=st["name"], value=str(st["id"]))
                for st in speaker["styles"][:25]
            ],
        )

        async def on_select(interaction: discord.Interaction):
            style_id = int(select.values[0])
            style_name = next(
                (st["name"] for st in speaker["styles"] if st["id"] == style_id), "?")
            tts.set_user_voice(interaction.user.id, style_id)
            await interaction.response.edit_message(
                content=f"✅ 声を **{speaker['name']}（{style_name}）** に設定しました。", view=None)

        select.callback = on_select
        self.add_item(select)

        back = discord.ui.Button(label="◀ キャラクター選択に戻る", row=1)

        async def on_back(interaction: discord.Interaction):
            await interaction.response.edit_message(
                content=PICKER_PROMPT,
                embed=picker_embed(speakers),
                view=CharacterPickerView(speakers),
            )

        back.callback = on_back
        self.add_item(back)
