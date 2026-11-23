"""打包脚本：PyInstaller onedir，产出「视频下载助手/」绿色文件夹。

用法：
  python prepare_bin.py        # 先把 ffmpeg/ffprobe 放进 bin/（本机需有 ffmpeg）
  python build.py              # 打包到 dist/视频下载助手/

产物结构：
  dist/视频下载助手/
    视频下载助手.exe           # 双击启动
    _internal/                 # Python 运行时 + 依赖 + web/ + core/
    config.json                # 首次运行生成（key 存这里）
    asset_cache.json           # 资产库缓存
    bin/ffmpeg.exe             # 从源 bin/ 复制（prepare_bin 准备）
    bin/ffprobe.exe
    数据目录.txt               # 可选：写视频数据目录绝对路径，改ROOT指向
"""
import os, sys, shutil, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# 支持环境变量覆盖产物名与 dist 根目录（旧产物被占用/锁定时换名打包）
APP_NAME = os.environ.get("BUILD_APP_NAME", "视频下载助手")
_DIST_ROOT = os.environ.get("BUILD_DIST_ROOT", "dist")
# 优先使用 CI 注入的 git tag 版本（如 v1.0.6 -> 1.0.6.0），本地默认 1.0.0.0
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0.0").strip().lstrip("v")
if not all(part.isdigit() for part in APP_VERSION.split(".")):
    raise ValueError(f"版本号只能包含数字和点：{APP_VERSION}")
APP_VERSION = ".".join((APP_VERSION.split(".") + ["0", "0", "0"])[:4])
ICON = os.path.join(HERE, "assets", "app.ico")
VERSION_FILE = os.path.join(HERE, "assets", "version_info.txt")
DIST = os.path.join(HERE, _DIST_ROOT, APP_NAME)
SPEC = os.path.join(HERE, APP_NAME + ".spec")

HIDDEN_IMPORTS = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "openai",
    "httpx",
    "playwright",
    "playwright.sync_api",
    "yt_dlp", "yt_dlp.extractor.youtube",
    "core", "core.dy_random", "core.dy_audio",
]


def make_spec():
    # 用 --add-data 方式，避免手写 spec 的路径转义坑
    sep = os.pathsep
    datas = [
        (os.path.join(HERE, "web"), "web"),
        (os.path.join(HERE, "core"), "core"),
        (os.path.join(HERE, "assets"), "assets"),  # 图标：webview.start(icon=...) 用
        (os.path.join(HERE, "bin"), "bin"),  # ffmpeg/ffprobe（可能不存在，跳过）
    ]
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--windowed",
        "--name", APP_NAME,
        "--distpath", os.path.dirname(DIST),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
    ]
    for src, dst in datas:
        if os.path.isdir(src):
            cmd += ["--add-data", f"{src}{sep}{dst}"]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    # 平台图标：Windows 用 ico+版本信息；macOS 用 icns（version-file 仅 Windows 支持）
    if sys.platform == "win32":
        if os.path.isfile(ICON):
            cmd += ["--icon", ICON]
        if os.path.isfile(VERSION_FILE):
            cmd += ["--version-file", VERSION_FILE]
    elif sys.platform == "darwin":
        icns = os.path.join(HERE, "assets", "app.icns")
        if os.path.isfile(icns):
            cmd += ["--icon", icns]
    else:
        png = os.path.join(HERE, "assets", "app_256.png")
        if os.path.isfile(png):
            cmd += ["--icon", png]
    cmd.append(os.path.join(HERE, "main.py"))
    return cmd


def write_version_file():
    """生成 PyInstaller 版本信息文件（exe 右键属性可见：产品名/版本/描述）。"""
    v = [int(x) for x in APP_VERSION.split(".")]
    while len(v) < 4:
        v.append(0)
    vers = ",".join(str(x) for x in v[:4])
    txt = f"""# UTF-8
# 自动生成于 build.py，勿手工编辑；版本号由 APP_VERSION 环境变量或默认值决定
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({vers}),
    prodvers=({vers}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', '家庭自用'),
        StringStruct('FileDescription', '对话式视频下载工具：发链接或一句话即可下载 B站/抖音/微博等视频'),
        StringStruct('FileVersion', '{APP_VERSION}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('LegalCopyright', '仅供个人与家庭学习使用'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{APP_NAME}'),
        StringStruct('ProductVersion', '{APP_VERSION}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(txt)
    print("  已生成版本信息:", VERSION_FILE)


def main():
    if not os.path.isfile(ICON):
        print("[warn] 缺少图标 assets/app.ico，运行 make_icon.py 生成")
    write_version_file()
    print("[1/3] PyInstaller 打包...")
    cmd = make_spec()
    print("  " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        print("[error] 打包失败")
        sys.exit(r.returncode)

    # macOS windowed 产物是 .app bundle，后处理文件放进 bundle 内
    bundle = DIST + ".app" if sys.platform == "darwin" else DIST
    if not os.path.isdir(bundle):
        print(f"[error] 未找到产物目录 {bundle}")
        sys.exit(1)

    print("[2/3] 复制 ffmpeg/ffprobe 到产物 bin/ ...")
    src_bin = os.path.join(HERE, "bin")
    dst_bin = os.path.join(bundle, "bin")
    if os.path.isdir(src_bin) and sys.platform != "darwin":
        # macOS 走 --add-data 进 bundle 资源目录，无需复制
        os.makedirs(dst_bin, exist_ok=True)
        for f in os.listdir(src_bin):
            s, d = os.path.join(src_bin, f), os.path.join(dst_bin, f)
            if os.path.isfile(s):
                shutil.copy2(s, d)
                print(f"  {f} ({os.path.getsize(s)/1048576:.0f}MB)")
    elif sys.platform == "darwin":
        print("  macOS：bin 已随 --add-data 进入 bundle 资源目录")
    else:
        print("  [warn] 源 bin/ 不存在：产物将依赖目标机器 PATH 中的 ffmpeg")

    print("[3/3] 写数据目录指针...")
    ptr = os.path.join(bundle, "数据目录.txt")
    if not os.path.exists(ptr):
        # 留空说明：不写则 ROOT 默认取 exe 所在目录；如需集中存放视频，填入绝对路径
        with open(ptr, "w", encoding="utf-8") as f:
            f.write("# 视频数据目录说明\n"
                    "# 留空：视频存到本 exe 所在目录\n"
                    "# 或填绝对路径（如 D:\\视频下载），所有视频/cookie/缓存放那里\n")
        print("  已生成 数据目录.txt 模板（留空则用 exe 目录）")

    print("[4/4] 不预置 config.json（避免把本机 API Key 打进分发包）...")
    print("  产物首次运行时请在设置页自行填写 API Key")

    print(f"\n[done] 产物：{DIST}")
    print("  双击 视频下载助手.exe 启动；首次运行请在设置页填写 API Key")


if __name__ == "__main__":
    main()
