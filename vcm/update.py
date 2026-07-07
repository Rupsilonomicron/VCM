"""
GitHub Releases を使った更新確認と自動アップデート。

最新リリースのタグを現在の __version__ と比較する。参照先は通常 DEFAULT_REPO で、
config.json の github_repo（"owner/repo" または GitHub の URL）で上書きできる。
オフライン・API エラー時は静かに何もしない（本体機能に影響させない）。

自動アップデート（配布版のみ）:
  リリースの zip アセットをダウンロード → 一時フォルダに展開 → 更新用 bat を
  切り離して起動 → 本体終了。bat が本体プロセスの終了を待ってから robocopy で
  ファイルを上書きし、start_bot.bat で再起動する。config.json / presets.json /
  tts.json は zip に含まれないため上書きされない。
"""

import os
import re
import subprocess
import tempfile
import zipfile
from typing import Optional

import aiohttp

from vcm import __version__
from vcm import config as vcm_config

DEFAULT_REPO = "Rupsilonomicron/VCM"
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))

# 更新用 bat。日本語Windowsのcmdで文字化けしないよう純ASCIIで書き、
# 日本語を含み得るパスは環境変数（VCM_*）経由で渡す。
_UPDATER_BAT = r"""@echo off
setlocal
set /a tries=0
:waitloop
tasklist /FI "PID eq %VCM_PID%" 2>nul | find "%VCM_PID%" >nul
if errorlevel 1 goto copy
set /a tries+=1
if %tries% geq 90 goto copy
ping -n 2 127.0.0.1 >nul
goto waitloop
:copy
robocopy "%VCM_SRC%" "%VCM_DST%" /E /IS /IT /R:3 /W:2 > "%VCM_DST%\update.log" 2>&1
start "VCM" /D "%VCM_DST%" cmd /c "%VCM_DST%\start_bot.bat"
rd /s /q "%VCM_TMP%" >nul 2>&1
(goto) 2>nul & del "%~f0"
"""


def is_dist(root: str = ROOT_DIR) -> bool:
    """配布版（同梱Python）で動いているか。ソース実行なら False。"""
    return os.path.exists(os.path.join(root, "python", "python.exe"))


def _parse_version(text: str) -> Optional[tuple]:
    """"v1.2.3" / "1.2.3" → (1, 2, 3)。解釈できなければ None。"""
    m = re.match(r"v?(\d+(?:\.\d+)*)", str(text).strip())
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def _repo_slug() -> Optional[str]:
    """設定値（無ければ既定リポジトリ）を "owner/repo" 形式に正規化。URL でも可。"""
    raw = vcm_config.get_github_repo() or DEFAULT_REPO
    if not raw:
        return None
    m = re.search(r"github\.com/([^/\s]+/[^/\s]+)", raw)
    slug = (m.group(1) if m else raw).strip().strip("/")
    slug = re.sub(r"\.git$", "", slug)
    return slug if re.fullmatch(r"[\w.-]+/[\w.-]+", slug) else None


async def check_for_update() -> Optional[dict]:
    """新しいリリースがあれば {"version", "url"} を返す。無ければ None。"""
    slug = _repo_slug()
    if not slug:
        return None
    url = f"https://api.github.com/repos/{slug}/releases/latest"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                    url, headers={"Accept": "application/vnd.github+json"}) as resp:
                if resp.status != 200:  # リリース未作成(404)やレート制限(403)は無視
                    return None
                data = await resp.json()
    except Exception:
        return None
    latest = _parse_version(data.get("tag_name") or "")
    current = _parse_version(__version__)
    if not latest or not current or latest <= current:
        return None
    asset_url = None
    for asset in data.get("assets", []):
        if re.fullmatch(r"VCM.*\.zip", str(asset.get("name")), re.IGNORECASE):
            asset_url = asset.get("browser_download_url")
            break
    return {
        "version": str(data.get("tag_name")),
        "url": data.get("html_url") or f"https://github.com/{slug}/releases",
        "asset_url": asset_url,
        "can_apply": bool(asset_url) and is_dist(),
    }


async def prepare_and_launch(info: dict, *, dst: str = ROOT_DIR, launch: bool = True) -> str:
    """リリース zip をダウンロード・展開し、更新用 bat を切り離して起動する。

    呼び出し後に本体を終了させると、bat がファイルを置き換えて再起動する。
    失敗時は例外（一時フォルダは残さない）。戻り値は作業フォルダのパス。
    """
    asset_url = info.get("asset_url")
    if not asset_url:
        raise RuntimeError("リリースに配布用 zip が見つかりません")

    tmp = tempfile.mkdtemp(prefix="vcm_update_")
    try:
        zip_path = os.path.join(tmp, "update.zip")
        timeout = aiohttp.ClientTimeout(total=1800, connect=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(asset_url) as resp:
                resp.raise_for_status()
                with open(zip_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        f.write(chunk)

        extract_dir = os.path.join(tmp, "extracted")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        # zip 直下の1フォルダ（VCM）が新ファイル一式。直下にファイルが並ぶ形式にも対応
        entries = os.listdir(extract_dir)
        if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
            src = os.path.join(extract_dir, entries[0])
        else:
            src = extract_dir
        if not os.path.exists(os.path.join(src, "start_bot.bat")):
            raise RuntimeError("zip の中身が VCM の配布物ではありません")

        if launch:
            # bat は作業フォルダの外に置く（bat 自身が VCM_TMP を削除するため中に置けない）
            fd, bat_path = tempfile.mkstemp(prefix="vcm_updater_", suffix=".bat")
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(_UPDATER_BAT)
            env = {
                **os.environ,
                "VCM_PID": str(os.getpid()),
                "VCM_SRC": src,
                "VCM_DST": dst,
                "VCM_TMP": tmp,
                # 再起動時はブラウザを開かない（既存タブが自動で再読み込みして引き継ぐ）
                "VCM_NO_BROWSER": "1",
            }
            subprocess.Popen(
                ["cmd", "/c", bat_path], env=env, cwd=tmp, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        return tmp
    except Exception:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        raise
