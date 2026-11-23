"""检查 GitHub Release 新版本，并把匹配的桌面安装包下载到本地。

只下载不安装：避免运行中覆盖程序、macOS Gatekeeper、Linux/麒麟 root 提权等问题。
"""
import json
import os
import platform
import sys
import urllib.request

import build

REPO = "dxhklck/downloader"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 10


def build_info():
    """读取打包时写入的构建信息；源码模式未提供时回退到 build.py。"""
    import json
    import config
    path = os.path.join(config._RES_DIR, "assets", "build_info.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("version"):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {
        "version": build.APP_VERSION,
        "author": build.AUTHOR,
        "author_url": build.AUTHOR_URL,
    }


def current_version():
    """返回形如 1.0.6 的当前版本（构建里的 1.0.6.0 去掉末位）。"""
    parts = str(build_info().get("version") or build.APP_VERSION).split(".")
    return ".".join(parts[:3])


def _version_tuple(text):
    parts = []
    for p in str(text or "").lstrip("vV").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_newer(latest, current=None):
    """latest 是否严格大于 current，按数字段逐个比较。"""
    a = _version_tuple(latest)
    b = _version_tuple(current if current is not None else current_version())
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)) > b + (0,) * (size - len(b))


def is_kylin_arm64():
    machine = platform.machine().lower()
    if machine not in ("aarch64", "arm64"):
        return False
    if sys.platform.startswith("linux") and os.path.exists("/etc/kylin-release"):
        return True
    return False


def select_asset(assets, plat=None, machine=None):
    """按平台和架构选安装包；麒麟 ARM64 无云端产物返回 None。"""
    names = [a.get("name", "") for a in assets if isinstance(a, dict)]
    plat = plat or sys.platform
    machine = (machine or platform.machine()).lower()

    if plat == "win32":
        suffixes = (".exe",)
    elif plat == "darwin":
        suffixes = (".dmg",)
    elif plat.startswith("linux"):
        if machine in ("aarch64", "arm64"):
            return None  # 麒麟 ARM64 需在目标机本地构建，Release 里的 deb 是 x86_64
        suffixes = (".deb",)
    else:
        return None
    for name in names:
        low = name.lower()
        if low.endswith(suffixes) and "source" not in low:
            return name
    return None


def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "video-downloader-updater"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def check_update():
    """返回检查结果与可用安装包信息，不抛异常到调用层。"""
    try:
        latest = _http_json(API_URL)
        tag = str(latest.get("tag_name", ""))
        assets = latest.get("assets", [])
        asset = select_asset(assets)
        return {
            "ok": True,
            "current": current_version(),
            "latest": tag.lstrip("vV"),
            "tag": tag,
            "has_update": is_newer(tag),
            "asset": asset,
            "page": latest.get("html_url", f"https://github.com/{REPO}/releases/latest"),
            "note": ("麒麟 ARM64 无预编译包，请在目标机运行 build_kylin_arm64.sh 构建"
                     if is_kylin_arm64() else ""),
        }
    except Exception as e:
        return {"ok": False, "message": "检查更新失败，请检查网络后重试", "detail": str(e)[:300]}


def download_update(asset, page=None):
    """把安装包下载到用户下载目录；返回路径供前端展示。"""
    if not asset or not isinstance(asset, str) or "/" in asset or "\\" in asset:
        return {"ok": False, "message": "没有可用的安装包"}
    if not page or not isinstance(page, str) or not page.startswith("https://github.com/"):
        return {"ok": False, "message": "发布页地址无效"}
    url = f"https://github.com/{REPO}/releases/download/{page.rstrip('/').split('/')[-1]}/{asset}"
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(downloads, exist_ok=True)
    target = os.path.join(downloads, asset)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "video-downloader-updater"})
        with urllib.request.urlopen(req, timeout=60) as r, open(target, "wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        return {"ok": True, "message": "已下载安装包", "path": target}
    except Exception as e:
        return {"ok": False, "message": "下载失败，可到发布页手动下载", "detail": str(e)[:300],
                "page": page}
