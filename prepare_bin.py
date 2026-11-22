"""打包准备：把 ffmpeg/ffprobe 复制到 downloader/bin/，让打包产物自包含。

用法：python prepare_bin.py
- 若 bin/ 下已有则跳过（可手动替换为更小的 essentials 版以减小体积）
- 否则从 PATH、WinGet 常见安装位置搜索
- 目标机器若没有 ffmpeg，打包前需在有 ffmpeg 的机器上跑本脚本
"""
import os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "bin")


def find_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    # 1. PATH
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d.strip().strip('"'), exe)
        if os.path.isfile(p):
            return p
    # 2. WinGet 常见位置
    if os.name == "nt":
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
        if os.path.isdir(base):
            for pkg in os.listdir(base):
                if "ffmpeg" not in pkg.lower():
                    continue
                for root, _, files in os.walk(os.path.join(base, pkg)):
                    if exe in files:
                        return os.path.join(root, exe)
    return None


def main():
    os.makedirs(BIN, exist_ok=True)
    total = 0
    for name in ("ffmpeg", "ffprobe"):
        exe = name + (".exe" if os.name == "nt" else "")
        dst = os.path.join(BIN, exe)
        if os.path.isfile(dst):
            print(f"[skip] {exe} 已在 bin/")
            total += os.path.getsize(dst)
            continue
        src = find_tool(name)
        if not src:
            print(f"[warn] 未找到 {exe}：请在有 ffmpeg 的机器上重跑本脚本，或手动放入 bin/{exe}")
            continue
        print(f"[copy] {src} -> {dst}")
        shutil.copy2(src, dst)
        total += os.path.getsize(dst)
    if total:
        print(f"[done] bin/ 合计 {total/1048576:.0f} MB")


if __name__ == "__main__":
    main()
