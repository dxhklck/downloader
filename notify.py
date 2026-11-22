"""下载完成系统通知：Windows 尽力而为，失败静默。"""
import os
import subprocess


def notify(title, message):
    if os.name != "nt":
        return False
    ps = (
        "$ErrorActionPreference='Stop';"
        "[void][Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle=$args[0];$n.BalloonTipText=$args[1];"
        "$n.Visible=$true;$n.ShowBalloonTip(5000);Start-Sleep -Seconds 2;$n.Dispose()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps, title, message],
                       timeout=8, capture_output=True, check=False)
        return True
    except Exception:
        return False
