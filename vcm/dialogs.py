"""ネイティブのフォルダ選択ダイアログ（Windows）。

VCM はローカルアプリなので、サーバー側（＝利用者の PC）で OS 標準の
フォルダ選択ダイアログを開き、選ばれた絶対パスを受け取る。
（ブラウザのファイル選択では実際の絶対パスが取得できないため）

配布版の埋め込み Python には tkinter が含まれないので、PowerShell の
FolderBrowserDialog を使う（追加依存なし・Windows 標準）。
"""

import asyncio
import os
import subprocess
from typing import Optional

# 前面表示のため透明なオーナーフォームを owner にして ShowDialog する。
# 選択パスは UTF-8 で標準出力へ書き、Python 側で decode する。
_PS_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = 'VOICEVOX のインストール先フォルダを選んでください'
$dlg.ShowNewFolderButton = $false
if ($env:VCM_DIALOG_START) { try { $dlg.SelectedPath = $env:VCM_DIALOG_START } catch {} }
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Show()
$owner.Activate()
$result = $dlg.ShowDialog($owner)
$owner.Dispose()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dlg.SelectedPath) }
"""


async def pick_folder(initial: Optional[str] = None) -> Optional[str]:
    """フォルダ選択ダイアログを開き、選ばれた絶対パスを返す。
    キャンセル・失敗時は None（ダイアログを開けない環境でも例外を投げない）。"""
    def run() -> Optional[str]:
        env = dict(os.environ)
        if initial:
            env["VCM_DIALOG_START"] = initial
        else:
            env.pop("VCM_DIALOG_START", None)
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", _PS_SCRIPT],
                capture_output=True, env=env, timeout=300,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return None
        path = proc.stdout.decode("utf-8", "replace").strip()
        return path or None

    return await asyncio.get_running_loop().run_in_executor(None, run)
