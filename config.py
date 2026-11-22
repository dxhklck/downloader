"""下载工具配置：路径、LLM 供应商模板、本地配置读写。

ROOT 解析优先级（换机器/打包后仍可用）：
1. config.json 的 root 字段（用户显式指定）
2. 源码模式：本文件所在目录的上一级（即 <数据目录>/downloader/config.py）
3. 打包模式：exe 旁的「数据目录.txt」里写的路径
4. 兜底：exe / 脚本所在目录
"""
import os, sys, json, threading

_SAVE_LOCK = threading.Lock()
_IS_FROZEN = getattr(sys, "frozen", False)
# 资源目录（web/、core/、bin/）：打包后在 _MEIPASS，源码模式为本文件目录
_RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
# 可写目录（config.json、缓存）：打包后用 exe 所在目录，保证重启不丢
_APP_DIR = os.path.dirname(sys.executable) if _IS_FROZEN else os.path.dirname(os.path.abspath(__file__))
_HERE = _APP_DIR
CONFIG_FILE = os.path.join(_APP_DIR, "config.json")


def _read_config_raw():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _looks_like_data_root(path):
    """判断某目录是否像数据根目录（含任一已知视频分类目录）。"""
    if not path or not os.path.isdir(path):
        return False
    for d in ("动漫", "抖音", "凡人修仙传", "搞笑短视频", "热门"):
        if os.path.isdir(os.path.join(path, d)):
            return True
    return False


def _detect_root():
    # 1. 显式配置
    r = _read_config_raw().get("root")
    if r and os.path.isdir(r):
        return os.path.abspath(r)
    # 2. 源码模式：<数据目录>/downloader/config.py
    parent = os.path.dirname(_HERE)
    if _looks_like_data_root(parent):
        return parent
    # 3. 打包模式：exe 旁的指针文件
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else _HERE
    ptr = os.path.join(exe_dir, "数据目录.txt")
    if os.path.exists(ptr):
        try:
            with open(ptr, encoding="utf-8") as f:
                lines = [x.strip() for x in f if x.strip() and not x.strip().startswith("#")]
            r = lines[0] if lines else ""
            if r and os.path.isdir(r):
                return os.path.abspath(r)
        except Exception:
            pass
    # 4. 兜底：exe / 脚本所在目录
    return exe_dir


ROOT = _detect_root()

# 数据目录
DIR_BILI = os.path.join(ROOT, "动漫")
DIR_DOUYIN = os.path.join(ROOT, "抖音")
DIR_FUNNY = os.path.join(ROOT, "搞笑短视频")

# cookie（沿用现有）
COOKIE_BILI = os.path.join(ROOT, "www.bilibili.com_cookies.txt")
COOKIE_DOUYIN = os.path.join(ROOT, "www.douyin.com_cookies.txt")

# B站 ep 列表与抖音 profile
EP_LIST = os.path.join(ROOT, "_ep_list.json")
PROFILE_DOUYIN = os.path.join(DIR_DOUYIN, "_chrome_profile")

# 小红书：独立登录 profile + 自动导出的 cookie 文件（都放可写目录，重启不丢）
PROFILE_XHS = os.path.join(_APP_DIR, "xhs_profile")
XHS_COOKIE_FILE = os.path.join(_APP_DIR, "xhs_cookies.txt")

# 通用站点登录：统一 profile + 统一 cookie 文件（可含多个站点域名，yt-dlp 全站通用）
PROFILE_SITE = os.path.join(_APP_DIR, "site_profile")
COOKIES_FILE = os.path.join(_APP_DIR, "cookies.txt")

# ffmpeg / ffprobe：随包 bin/ 优先，PATH 回退（见 ffmpeg_exe/ffprobe_exe）
def _tool_exe(name):
    """定位 ffmpeg/ffprobe：优先随包 bin/（exe 旁或资源目录），回退 PATH。"""
    if os.name == "nt":
        name = name + ".exe"
    for base in (_HERE, _RES_DIR):  # exe 旁 与 _MEIPASS（mac bundle 数据落点）
        bundled = os.path.join(base, "bin", name)
        if os.path.isfile(bundled):
            return bundled
    return name


def ffmpeg_exe():
    return _tool_exe("ffmpeg")


def ffprobe_exe():
    return _tool_exe("ffprobe")


# OpenAI 兼容供应商模板（均为国内直连端点）
PROVIDERS = {
    "zhipu": {"label": "智谱 GLM（glm-4-flash 免费）", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "deepseek": {"label": "DeepSeek（deepseek-chat）", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "kimi": {"label": "月之暗面 Kimi（moonshot-v1-8k）", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "qwen": {"label": "阿里通义千问（qwen-plus）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
}


def load():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"provider": "zhipu", "api_key": "", "model": "", "out_dir": ROOT, "root": ROOT}


def save(cfg):
    with _SAVE_LOCK:
        merged = load()
        merged.update(cfg)
        tmp = CONFIG_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)  # 原子替换
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def proxy_url():
    """返回用户配置的代理地址（给 yt-dlp 用），未配置返回空串。"""
    return (load().get("proxy") or "").strip()


def system_proxy_url():
    """探测 Windows 系统代理（注册表），返回 http://host:port；未开启返回空串。
    绕开环境变量注入的代理，直接读用户代理软件设置的系统代理。
    """
    if os.name != "nt":
        return ""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled = winreg.QueryValueEx(k, "ProxyEnable")[0]
        server = winreg.QueryValueEx(k, "ProxyServer")[0]
        winreg.CloseKey(k)
        if not enabled or not server:
            return ""
        server = server.strip()
        # ProxyServer 可能是 "http=host:port;https=host:port" 形式，取 http
        if "=" in server:
            for part in server.split(";"):
                if "=" in part:
                    scheme, addr = part.split("=", 1)
                    if scheme.strip().lower() in ("http", "https"):
                        server = addr.strip()
                        break
        if "://" not in server:
            server = "http://" + server
        return server
    except Exception:
        return ""


def xhs_cookie():
    """返回用户配置的小红书 cookie 字符串（给 yt-dlp 用）。"""
    return (load().get("xhs_cookie") or "").strip()


def llm_params():
    """返回 (api_key, base_url, model)。"""
    cfg = load()
    p = PROVIDERS.get(cfg.get("provider", "zhipu"), PROVIDERS["zhipu"])
    return cfg.get("api_key", ""), p["base_url"], cfg.get("model") or p["model"]
