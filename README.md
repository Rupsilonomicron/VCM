# VCM — VoiceChatMover v1.2.0

大会やイベントで、メインVC ⇄ チーム別VC の分散・集合をブラウザGUIから素早く行うための
Discord bot（ローカル起動・自分用）。

## できること

**VC編成**
- サーバー内のVC一覧・接続ユーザー（名前＆アイコン）・チームを**リアルタイム表示**
- メンバーカードを**ドラッグ＆ドロップ**（VC移動／チーム割当・1人1チーム）
- チームヘッダをVC列へドラッグでチーム**一括移動**
- メインVC設定＋**集合・散開**、チーム編成の**プリセット保存／呼び出し**
- **参加希望の受付**: GUIの「✋ 参加希望を募る」でメインVCのチャットに参加希望ボタンを設置。
  メインVC接続中のユーザーがボタンを押すと本人だけに見えるチーム選択が表示され、
  選んだチームに所属（選択後または60秒放置でメッセージは自動的に消える）
- 複数サーバー対応（ヘッダのプルダウンで切替）

**読み上げ（VOICEVOX 検出時のみ有効）**
- **メインVC を設定すると bot が自動入室**（変更で移動・解除で退出）→ 入室VC の**内蔵チャット**を読み上げ
- `/voice` コマンドでユーザーごとの声設定（キャラ→スタイルの段階選択、tts.json に永続化）
- **辞書**（単語→読み、サーバーごと）、**スキップ／キュー消去**、テスト読み上げ（すべてGUI）
- VOICEVOX エンジンは自動検出・自動起動（未検出時は VC 編成機能のみ）

## セットアップ

1. **Discord Bot を作成**
   - https://discord.com/developers/applications → New Application → Bot
   - **Privileged Gateway Intents** で `SERVER MEMBERS INTENT` と `MESSAGE CONTENT INTENT` を ON
   - Bot Token をコピー
   - OAuth2 → URL Generator で `bot` + `applications.commands` スコープ、
     権限 `View Channels` `Send Messages` `Connect` `Speak` `Move Members` を付与し、サーバーに招待

2. **依存をインストール**（このフォルダで）
   ```
   py -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. **起動**
   ```
   start_bot.bat
   ```
   ブラウザで http://127.0.0.1:8765 が開きます。

4. **トークンを設定**（初回のみ）
   - 初回起動時はトークン設定画面が自動で開くので、Bot トークンを貼り付けて「保存して接続」
   - トークンはこの PC の `config.json` に保存され、次回からは自動で接続されます
   - 変更・削除はヘッダの「⚙ トークン設定」からいつでも可能
   - 初期選択サーバーや待受ポートを変えたい場合は `config.json` に
     `"guild_id"` / `"host"` / `"port"` を追記（省略時 127.0.0.1:8765）。
     VOICEVOX のインストール先が特殊な場合は `"voicevox_path"` で指定

## 制約（Discord 仕様）

- 移動できるのは**すでにVCに接続しているユーザーのみ**。VCにいない人は呼び出せません。
- bot に `Move Members` 権限と対象VCへの閲覧/接続権が必要です。

## 構成

```
VCM/
  vcm/
    main.py          エントリ（uvicorn 起動、トークンがあれば bot も起動）
    runner.py        bot のライフサイクル管理（トークン検証・起動・再起動）
    config.py        ローカル設定（config.json にトークンを保存）
    discord_bot.py   VC状態の取得と移動操作、読み上げのVC入退室、/voice コマンド
    voicevox.py      VOICEVOX エンジンの検出・自動起動・音声合成（FFmpeg 不要）
    tts.py           読み上げキュー・テキスト前処理・辞書/声設定の永続化（tts.json）
    server.py        FastAPI（WebSocket + REST、チーム状態を保持）
  web/
    index.html / app.js / style.css   ブラウザGUI
  dist_files/        配布物専用ファイルの原本（配布先向け README・同梱Python用 start_bot.bat）
  build_dist.bat     配布物ビルド（下記）
  build_dist.ps1     ビルドの実処理
  start_bot.bat
  requirements.txt
  config.json        （初回のトークン設定時に自動生成・配布物には含めない。
                       guild_id / host / port / voicevox_path もここで設定可能）
  tts.json           読み上げの声設定・辞書（自動生成・配布物には含めない）
```

## 配布物のビルド

ソース（vcm/ や web/）を修正したら `build_dist.bat` をダブルクリック。
`..\配布用\VCM` と `..\配布用\VCM.zip` を最新のソースで作り直します。

- 埋め込み Python（3.12.10）は無ければ自動ダウンロード。依存は requirements.txt が
  変わったときだけ再インストール（`python\Lib\site-packages` へ同梱）
- `config.json` / `.env` / `presets.json` / `tts.json` / `__pycache__` は配布物から自動除去
- 配布先向けの README と start_bot.bat は `dist_files/` の内容が使われる
  （配布物の文面を直したいときは `dist_files/` 側を編集）
