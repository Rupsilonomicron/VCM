"""
GitHub Releases を使った更新確認。

最新リリースのタグを現在の __version__ と比較する。参照先は通常 DEFAULT_REPO で、
config.json の github_repo（"owner/repo" または GitHub の URL）で上書きできる。
オフライン・API エラー時は静かに何もしない（本体機能に影響させない）。
"""

import re
from typing import Optional

import aiohttp

from vcm import __version__
from vcm import config as vcm_config

DEFAULT_REPO = "Rupsilonomicron/VCM"


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
    return {
        "version": str(data.get("tag_name")),
        "url": data.get("html_url") or f"https://github.com/{slug}/releases",
    }
