"""
VCM のローカル設定 (config.json)。

Discord トークンを含む全設定をここに保存する。配布物には含めない。
トークンは初回起動時に GUI から入力してもらう。

config.json のキー:
  discord_token  Bot トークン（GUI から設定・編集・削除）
  guild_id       初期選択サーバーの Guild ID（省略可。空なら最初のサーバー）
  host / port    ローカル GUI の待受アドレス（省略時 127.0.0.1:8765）
                 ※ host は 127.0.0.1 のまま使うこと。0.0.0.0 等に変えると
                   認証なしの全機能（トークン設定含む）がネットワークに公開される。
  voicevox_path  VOICEVOX の実行ファイル/フォルダ（省略時は自動検出）
  github_repo    更新確認先の GitHub リポジトリ "owner/repo"（省略時は既定リポジトリ）
"""

import json
import os
from typing import Optional

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json_atomic(path: str, data):
    """一時ファイルに書いてから置き換える（書き込み中のクラッシュで設定を失わない）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def save_config(config: dict):
    save_json_atomic(CONFIG_PATH, config)


# --- トークン -----------------------------------------------------------------
def get_token() -> Optional[str]:
    return (load_config().get("discord_token") or "").strip() or None


def save_token(token: str):
    config = load_config()
    config["discord_token"] = token.strip()
    save_config(config)


def delete_token():
    config = load_config()
    if "discord_token" in config:
        del config["discord_token"]
        save_config(config)


def mask_token(token: str) -> str:
    if len(token) <= 10:
        return "*" * len(token)
    return f"{token[:4]}……{token[-4:]}"


def token_info() -> dict:
    """GUI 表示用。トークン本体は返さない。"""
    token = get_token()
    return {
        "saved": bool(token),
        "masked": mask_token(token) if token else None,
    }


# --- その他の設定 --------------------------------------------------------------
def get_guild_id() -> Optional[str]:
    return str(load_config().get("guild_id") or "").strip() or None


def get_host() -> str:
    return str(load_config().get("host") or "").strip() or "127.0.0.1"


def get_port() -> int:
    try:
        return int(load_config().get("port") or 8765)
    except (TypeError, ValueError):
        return 8765


def get_voicevox_path() -> Optional[str]:
    """VOICEVOX のインストール先が特殊な場合の手動指定（通常は自動検出）。"""
    return str(load_config().get("voicevox_path") or "").strip() or None


def get_github_repo() -> Optional[str]:
    """更新確認先の GitHub リポジトリ（"owner/repo" または URL）。"""
    return str(load_config().get("github_repo") or "").strip() or None
