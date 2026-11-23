"""LLM agent：OpenAI 兼容 function calling 循环。

把用户的自然语言理解成工具调用，执行后把结果回灌模型，直到任务完成。
API key 从 config.json 读取，不硬编码。
"""
import json, os, re, threading, inspect, itertools, uuid
from datetime import date

from openai import OpenAI

import config
import tools


# 历史裁剪阈值：超过后保留摘要 + 最近若干轮完整对话。
MAX_HISTORY_GROUPS = 8
MAX_HISTORY_CHARS = 16000
MAX_ANSWER_WAIT_SEC = 120


def _ok(msg, **data):
    return {"success": True, "message": msg, "data": data}


def _fail(msg, error_code="tool_error", retryable=True, **data):
    return {"success": False, "message": msg, "error_code": error_code,
            "retryable": retryable, "data": data}


def _normalize_result(result):
    """统一工具结果字段，前端和模型都按同一结构处理。"""
    if not isinstance(result, dict):
        return {"success": False, "message": str(result)[:200],
                "error_code": "invalid_result", "retryable": True, "data": {}}
    success = bool(result.get("success"))
    return {
        "success": success,
        "message": str(result.get("message", ""))[:500],
        "data": result.get("data", {}),
        "error_code": result.get("error_code") or ("ok" if success else "tool_error"),
        "retryable": bool(result.get("retryable", not success)),
    }


def _result_str(result, limit=1500):
    """工具结果回灌前裁剪；超大结果落盘并回传短预览。"""
    normalized = _normalize_result(result)
    s = json.dumps(normalized, ensure_ascii=False, default=str)
    if len(s) <= limit:
        return s
    try:
        directory = os.path.join(getattr(config, "_APP_DIR", "."), "logs", "tool_results")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, uuid.uuid4().hex + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, default=str)
        preview = {
            "success": normalized["success"],
            "message": normalized["message"] + "（结果过大，已保存完整内容）",
            "data": {"preview": s[:600], "file": path, "truncated": True},
            "error_code": normalized["error_code"],
            "retryable": normalized["retryable"],
        }
        return json.dumps(preview, ensure_ascii=False)
    except Exception:
        return s[:limit] + "…(已截断)"


# 工具定义（给 LLM 看的）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bili_status",
            "description": "查询B站番剧下载状态：总集数、已下集数、缺失哪些集。用户问'下完了吗''还差多少'时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bili_download",
            "description": "下载B站番剧的指定集数范围。用户说'下载第X到Y集'时用，集数必须是整数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "integer", "description": "起始集号"},
                    "end": {"type": "integer", "description": "结束集号"},
                    "workers": {"type": "integer", "description": "并发数，默认4"},
                },
                "required": ["start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "douyin_random",
            "description": "只在抖音平台随机刷视频：用户提到抖音/douyin，或完全没指定平台只说'随机刷N个''刷到什么下什么'时用。用户提到B站/哔哩哔哩时绝对不能用本工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "下载数量"},
                    "keywords": {"type": "array", "items": {"type": "string"},
                                 "description": "可选的内容关键词筛选，如['唱歌','翻唱']；不填则完全随机"},
                },
                "required": ["count"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "douyin_login",
            "description": "打开抖音登录窗口让用户扫码。用户说'登录抖音''重新登录'或下载报登录失效时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clean_temp",
            "description": "清理B站目录的临时碎片文件释放空间。用户说'清理临时文件''清理碎片'时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "查询当前下载任务进度。用户问'下载完了吗''进度怎样'时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "url_download",
            "description": "按任意平台视频网页链接下载，含 B站/抖音/YouTube。用户给出 http:// 或 https:// 链接时用这个工具，不要用按集下载或随机下载。支持 out_dir 指定资产根目录下的保存目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整的视频网页链接，以 http:// 或 https:// 开头"},
                    "quality": {"type": "string", "enum": ["best", "1080", "720"], "description": "画质档位，默认 best"},
                    "out_dir": {"type": "string", "description": "可选：相对资产根目录的保存目录，如 B站comfyui；留空用 网络视频"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_videos",
            "description": "按关键词跨平台搜索视频候选（B站/YouTube等），只列出结果不下载。用户说收集/找/搜某些主题或关键词的视频时用；结果会推给界面让用户点选下载。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词，如 信息科技 互联网"},
                    "platform": {"type": "string", "enum": ["all", "bili", "youtube"],
                                 "description": "平台：all 默认、bili 哔哩哔哩、youtube；用户没指定就用 all"},
                    "count": {"type": "integer", "description": "候选数量，默认8，最多12"},
                    "out_dir": {"type": "string", "description": "可选：下载时保存的相对目录，界面会使用"},
                    "quality": {"type": "string", "enum": ["best", "1080", "720"], "description": "下载画质，默认 best"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_plan",
            "description": "把复杂请求拆成2-6步执行计划，界面会显示步骤卡。仅在需要多步操作时使用，不要用它代替实际工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {"type": "array", "items": {"type": "string"},
                              "description": "有序步骤，每步一句话"},
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_choice",
            "description": "当信息不足时向用户提出2-4个明确选项，等待用户选择。不要询问API Key、Cookie或密码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "具体问题"},
                    "options": {"type": "array", "items": {"type": "string"},
                                "description": "2-4个互斥选项"},
                },
                "required": ["question", "options"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "asset_stats",
            "description": "查询资产库统计：视频总数、总大小、各分类数量。用户问'有多少视频''占多少空间'时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "asset_search",
            "description": "在资产库按关键词、扩展名、分类和最小大小查找视频，返回少量结果。用户问'某个视频在哪''找最近下的mp4'时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "文件名或相对路径关键词"},
                    "ext": {"type": "string", "description": "扩展名，如 .mp4"},
                    "category": {"type": "string", "description": "顶层分类名"},
                    "min_size_mb": {"type": "number", "description": "最小大小（MB）"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认20，上限20"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_folder",
            "description": "在资产根目录下创建文件夹，可创建多级子目录。只允许相对根目录的安全路径，不能删除或移动文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对资产根目录的文件夹路径，如 动漫/2026}"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_assets",
            "description": "刷新资产库索引并返回统计摘要。用户说'重新扫描资产''刷新资产库'时用。",
            "parameters": {
                "type": "object",
                "properties": {"force": {"type": "boolean", "description": "是否强制全量扫描"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reveal_asset",
            "description": "在文件管理器中定位并选中资产库里的视频文件。只允许资产库内的视频路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "资产库内视频的完整路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_asset",
            "description": "移动或重命名资产库内视频。目标必须在资产根目录内，且不能覆盖已存在文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "源视频完整路径"},
                    "dest": {"type": "string", "description": "目标视频完整路径"},
                },
                "required": ["src", "dest"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_asset",
            "description": "复制资产库内视频到另一个目录。目标必须在资产根目录内，且不能覆盖已存在文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "源视频完整路径"},
                    "dest": {"type": "string", "description": "目标视频完整路径"},
                },
                "required": ["src", "dest"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trash_asset",
            "description": "把资产库内视频移入应用内回收站，不做永久删除，可恢复。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "源视频完整路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restore_asset",
            "description": "从应用内回收站恢复视频到原路径；原路径被占用时拒绝覆盖。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "回收站项目名称"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "organize_preview",
            "description": "按规则生成批量整理预览，不实际改动文件。用于用户确认前展示将发生的变更。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string", "enum": ["ext", "date", "size", "category"], "description": "整理规则"},
                    "category": {"type": "string", "description": "ext规则下的扩展名，或category规则下的分类名"},
                    "value": {"type": "string", "description": "可选的目标父目录"},
                    "limit": {"type": "integer", "description": "最多预览数量，默认200"},
                },
                "required": ["rule"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "organize_apply",
            "description": "按 organize_preview 生成的一次性预览批量执行整理。",
            "parameters": {
                "type": "object",
                "properties": {
                    "preview_id": {"type": "string", "description": "整理预览 id"},
                },
                "required": ["preview_id"],
            },
        },
    },
]

# 函数名 -> 实际函数
FUNCS = {
    "bili_status": tools.bili_status,
    "bili_download": tools.bili_download,
    "douyin_random": tools.douyin_random,
    "douyin_login": tools.douyin_login,
    "clean_temp": tools.clean_temp,
    "get_state": tools.get_state,
    "url_download": tools.url_download,
    "search_videos": tools.search_videos,
    "make_plan": None,
    "ask_choice": None,
    "asset_stats": tools.asset_stats,
    "asset_search": tools.asset_search,
    "create_folder": tools.create_folder,
    "scan_assets": tools.scan_assets,
    "reveal_asset": tools.reveal_asset,
    "move_asset": tools.move_asset,
    "copy_asset": tools.copy_asset,
    "trash_asset": tools.trash_asset,
    "restore_asset": tools.restore_asset,
    "organize_preview": tools.organize_preview,
    "organize_apply": tools.organize_apply,
}

# 批量整理必须先预览再执行；预览工具本身不改动文件。
CONFIRM_BEFORE = {"organize_apply"}

# 这些工具由 Agent 自己处理，没有可重放的外部副作用。
NO_RETRY_TOOLS = {"get_state", "make_plan", "ask_choice"}

# 选择题不能诱导用户提供敏感信息；命中直接拒绝，不发起等待事件。
_SENSITIVE_CHOICE_WORDS = ("api key", "apikey", "cookie", "密码", "token", "密钥")

# 函数名 -> Schema 声明的参数名集合。LLM 只能传这些键，
# 不能借函数签名里额外的可选参数（如 out_dir/ep_path）越权写路径。
_SCHEMA_PARAMS = {
    t["function"]["name"]: set(t["function"]["parameters"].get("properties", {}))
    for t in TOOLS
}

# 平台关键词 → 该平台允许的工具前缀。用户明确提到平台时硬性约束工具选择，
# 不依赖 LLM 自觉（曾出现"B站随机下一首"被路由到 douyin_random）。
_PLATFORM_PREFIXES = (
    (("b站", "哔哩哔哩", "bilibili"), "bili_"),
    (("抖音", "douyin"), "douyin_"),
)
_PLATFORM_TOOLS = {"bili_status", "bili_download", "douyin_random", "douyin_login", "url_download", "search_videos"}


def _forced_platform(text):
    """从用户原话识别明确平台，返回允许的工具前缀；未明确则返回 None。"""
    low = (text or "").lower()
    for words, prefix in _PLATFORM_PREFIXES:
        if any(w in low for w in words):
            return prefix
    return None


def _has_video_link(text):
    return bool(re.search(r"https?://", str(text or ""), re.I))


def _tool_forbidden(forced, name, text=""):
    if not forced or name not in _PLATFORM_TOOLS:
        return False
    if name == "url_download" and _has_video_link(text):
        return False
    if name == "search_videos":
        return False
    return not name.startswith(forced)


SYSTEM = """你是视频下载助手。用户用中文告诉你要下载什么，你通过调用工具完成任务。
最重要的规则：先看用户提到的平台（B站/哔哩哔哩/bilibili 或 抖音/douyin），再选工具。
平台是硬约束：**提到 B站就绝不能调用任何 douyin_ 工具，提到抖音就绝不能调用任何 bili_ 工具。**
没有明确平台时才按下面的默认规则处理。

平台识别：
- 提到 B站/哔哩哔哩/bilibili → 按集数下载番剧用 bili_ 工具；按主题/关键词搜索收集视频用 search_videos(platform='bili')，搜索后让用户点选，不要反复追问。
- 提到抖音/douyin → 只能用 douyin_ 工具（douyin_random / douyin_login）。
- 两个平台都没提 → 按默认规则（下面第 3、4 条）处理。

默认规则：
1. 用户给出 http:// 或 https:// 开头的网页链接（YouTube、B站、抖音、微博等任意网站）→ 一律调 url_download，把完整链接原样传给 url 参数；即使句子里提到 B站/抖音也同样用链接下载，不要调用按集或随机工具。用户说放到某目录 → 传 out_dir（相对资产根目录）；没说目录就不传 out_dir。
2. 用户说下载番剧/集数（没给链接）→ 调 bili_download；集数范围从用户的话里提取。若只说剧名没给范围，先调 bili_status 查总集数再决定，不要臆测数字。
3. 用户要按主题/关键词收集、搜索、找视频（如"信息科技课程""互联网方面"）→ 调 search_videos，平台按用户说的选；用户没说平台就 platform='all'。搜索结果交给界面让用户点选下载，不要再追问关键词细节。
4. 用户说随机刷/刷到什么下什么（没给链接、没主题关键词）→ 调 douyin_random，数量从话里提取。
4a. 用户说唱歌/音乐类（没给链接）→ 调 douyin_random 并传 keywords=['唱歌','翻唱','现场','唱']。
5. 用户问当前任务进度/下完没 → 调 get_state；问某部番剧总共下了多少集 → 调 bili_status。
6. 用户说登录 → 调 douyin_login；但提到 B站登录时，文字回复"B站请在设置页通用登录里打开 bilibili.com 扫码"，不要调 douyin_login。
7. 用户说清理 → 调 clean_temp。

资产与协作规则：
8. 用户问资产库有多少、占多少空间 → asset_stats；找文件/问文件在哪 → asset_search；要重新扫描 → scan_assets；要在文件管理器中定位 → reveal_asset。
9. 用户要新建下载分类或文件夹 → create_folder；路径必须相对资产根目录。
10. 用户要移动/重命名视频 → move_asset；复制 → copy_asset；删除 → trash_asset（只进应用回收站）；从回收站恢复 → restore_asset。所有目标必须在资产根目录内，不能覆盖同名文件。
11. 用户要批量整理 → 必须先用 organize_preview 生成预览并把变更告诉用户；只有用户明确确认后才调用 organize_apply。单次移动/重命名/复制/删除不需要额外确认。
12. 复杂请求先 make_plan 展示步骤，再逐步执行；信息不足时用 ask_choice 给出2-4个选项，不要询问 Key/Cookie/密码。
13. 资产与文件夹工具与平台无关，不受 B站/抖音平台硬路由限制。
14. 每次只返回一个工具调用，等结果后再决定下一步。任务完成后用简洁中文汇报结果。
15. 如果工具返回失败（success=false），如实告诉用户原因，不要假装成功。
16. 用户表达模糊时不要连续追问：有主题就先 search_videos 给候选；有链接就直接 url_download；只有完全没有可执行线索时才用 ask_choice 给2-4个选项。
"""


def _shorten(value, limit=160):
    s = str(value) if value is not None else ""
    return s if len(s) <= limit else s[:limit] + "…"


def _dynamic_context():
    """只注入非敏感的运行状态，绝不把 Key/Cookie 内容喂给模型。"""
    parts = ["当前日期：" + date.today().isoformat()]
    root = getattr(config, "ROOT", "")
    if root:
        parts.append("资产根目录：" + str(root))
    try:
        state = tools.get_state()
        parts.append("当前任务：%s；进度 %s/%s；运行中：%s" % (
            _shorten(state.get("task") or "无"), state.get("done", 0),
            state.get("total", 0), bool(state.get("running"))))
        queue = state.get("queue") or []
        if queue:
            parts.append("排队任务：" + "；".join(
                "#%s %s" % (q.get("id"), _shorten(q.get("name"), 40)) for q in queue[:5]))
    except Exception:
        pass
    try:
        login = tools.get_login_status()
        parts.append("登录状态：" + "；".join(
            "%s=%s" % (k, v.get("status")) for k, v in login.items()
            if isinstance(v, dict)))
    except Exception:
        pass
    return "\n".join(parts)


def _message_dict(msg):
    """把 SDK 消息对象规范成可保存、可回灌的普通字典。"""
    if isinstance(msg, dict):
        return msg
    tool_calls = []
    for tc in getattr(msg, "tool_calls", None) or []:
        fn = getattr(tc, "function", None)
        tool_calls.append({
            "id": getattr(tc, "id", ""),
            "type": "function",
            "function": {
                "name": getattr(fn, "name", ""),
                "arguments": getattr(fn, "arguments", "{}"),
            },
        })
    data = {"role": getattr(msg, "role", "assistant"),
            "content": getattr(msg, "content", None)}
    if tool_calls:
        data["tool_calls"] = tool_calls
    return data


def _history_groups(messages):
    """按用户消息分组，保证组内 tool_use/tool_result 不分离。"""
    groups = []
    current = []
    for msg in messages:
        data = _message_dict(msg)
        if data.get("role") == "user" and current:
            groups.append(current)
            current = []
        current.append(data)
    if current:
        groups.append(current)
    return groups


def _make_summary(history):
    lines = []
    for msg in history:
        content = _message_dict(msg).get("content")
        if isinstance(content, str) and content.strip():
            lines.append("%s: %s" % (msg.get("role"), _shorten(content, 120)))
    return "；".join(lines[-24:])[:1200]


def _clip_history(history):
    groups = _history_groups(history)
    normalized = [_message_dict(m) for m in history]
    size = len(json.dumps(normalized, ensure_ascii=False))
    if len(groups) <= MAX_HISTORY_GROUPS and size <= MAX_HISTORY_CHARS:
        return normalized
    keep = groups[-MAX_HISTORY_GROUPS:]
    clipped = [m for group in keep for m in group]
    summary = _make_summary([m for group in groups[:-MAX_HISTORY_GROUPS] for m in group])
    if summary:
        clipped.insert(0, {"role": "user", "content": "此前对话摘要：" + summary})
    return clipped


class Agent:
    def __init__(self, emit):
        """emit(event, data) 用于向界面推送消息/工具调用。"""
        self.emit = emit
        self.client = None
        self.model = None
        self.history = []
        self.plan = []
        self._choice_lock = threading.Lock()
        self._choices = {}
        self._choice_seq = itertools.count(1)
        self._refresh_client()

    def _refresh_client(self):
        key, base, model = config.llm_params()
        self.model = model
        if key:
            self.client = OpenAI(api_key=key, base_url=base, timeout=120)
        else:
            self.client = None

    def configured(self):
        key, _, _ = config.llm_params()
        return bool(key)

    def answer_choice(self, answer_id, value):
        """前端选项回调：唤醒等待中的 ask_choice。"""
        with self._choice_lock:
            entry = self._choices.get(str(answer_id))
            if not entry:
                return {"ok": False, "message": "选项已过期或不存在"}
            options = entry["options"]
            text = str(value)
            if text not in options:
                return {"ok": False, "message": "请选择给出的选项", "options": options}
            if entry["answer"] is not None:
                return {"ok": False, "message": "该选项已提交，请勿重复回答"}
            entry["answer"] = text
            entry["event"].set()
        return {"ok": True, "message": "已记录选择", "answer": text}

    def _ask_choice(self, args):
        question = _shorten(args.get("question") or "请选择", 500)
        options = args.get("options") if isinstance(args.get("options"), list) else []
        options = [_shorten(x, 160) for x in options if str(x).strip()][:4]
        if len(options) < 2:
            return _fail("选择题至少需要2个选项", error_code="bad_arguments", retryable=False)
        sensitive = "、".join(w for w in _SENSITIVE_CHOICE_WORDS
                           if w in question.lower() or any(w in str(o).lower() for o in options))
        if sensitive:
            return _fail("选择题不能询问敏感信息（%s）" % sensitive,
                         error_code="sensitive_choice", retryable=False)

        answer_id = "c%s" % next(self._choice_seq)
        event = threading.Event()
        entry = {"event": event, "options": options, "answer": None}
        with self._choice_lock:
            self._choices[answer_id] = entry
        self.emit("ask_choice", {"id": answer_id, "question": question, "options": options})
        try:
            waited = event.wait(MAX_ANSWER_WAIT_SEC)
        finally:
            with self._choice_lock:
                self._choices.pop(answer_id, None)
        if not waited or not entry["answer"]:
            return _fail("用户未选择，超时", error_code="timeout", retryable=False)
        return _ok("用户选择了：" + entry["answer"], answer=entry["answer"], id=answer_id)

    def _make_plan(self, args):
        steps = args.get("steps") if isinstance(args.get("steps"), list) else []
        steps = [_shorten(x, 200) for x in steps if str(x).strip()][:6]
        if len(steps) < 2:
            return _fail("执行计划至少需要2个步骤", error_code="bad_arguments", retryable=False)
        self.plan = steps
        self.emit("plan", {"steps": steps})
        return _ok("已生成执行计划", steps=steps)

    def _organize_preview(self, args):
        result = _normalize_result(tools.organize_preview(
            rule=args.get("rule"),
            category=args.get("category"),
            value=args.get("value"),
            limit=args.get("limit", 200),
        ))
        if result["success"]:
            self.emit("organize_preview", result["data"])
            result["message"] += "；请在界面确认后再执行"
        return result

    def _search_videos(self, args):
        result = _normalize_result(tools.search_videos(
            keyword=args.get("keyword"),
            platform=args.get("platform", "all"),
            count=args.get("count", 8),
            out_dir=args.get("out_dir"),
            quality=args.get("quality", "best"),
        ))
        if result["success"]:
            self.emit("search_results", result["data"])
        return result

    def _execute_tool(self, name, args, forced, tool_call_id, user_text=""):
        import time
        started = time.monotonic()
        self.emit("tool", {"name": name, "args": args, "status": "running",
                           "tool_call_id": tool_call_id, "elapsed_ms": 0})
        if not isinstance(args, dict):
            result = _fail("工具参数格式错误，请重新调用", error_code="bad_arguments")
        elif _tool_forbidden(forced, name, user_text):
            platform = "B站" if forced == "bili_" else "抖音"
            result = _fail(
                f"用户明确指定{platform}，禁止调用 {name}。请改用{platform}的工具，或文字说明该平台不支持此需求。",
                error_code="platform_conflict")
        elif name == "make_plan":
            result = self._make_plan(args)
        elif name == "ask_choice":
            result = self._ask_choice(args)
        elif name == "organize_preview":
            result = self._organize_preview(args)
        elif name == "search_videos":
            result = self._search_videos(args)
        else:
            fn = FUNCS.get(name)
            if not callable(fn):
                result = _fail(f"未知工具 {name}", error_code="unknown_tool")
            else:
                schema_keys = _SCHEMA_PARAMS.get(name, set())
                signature_keys = set(inspect.signature(fn).parameters)
                call_args = {k: v for k, v in args.items() if k in schema_keys & signature_keys}
                try:
                    result = fn(**call_args)
                except Exception as e:
                    result = _fail(str(e)[:200], error_code="tool_exception")
        result = _normalize_result(result)
        elapsed = int((time.monotonic() - started) * 1000)
        self.emit("tool_done", {"name": name, "args": args,
                                "status": "ok" if result["success"] else "error",
                                "tool_call_id": tool_call_id, "elapsed_ms": elapsed,
                                "error_code": result["error_code"],
                                "retryable": result["retryable"]})
        return result

    @staticmethod
    def _retryable_api_error(e):
        code = getattr(e, "status_code", None)
        return code is None or code == 429 or (isinstance(code, int) and 500 <= code < 600)

    def chat(self, user_text):
        """处理一条用户消息。在后台线程调用，通过 emit 推送过程。"""
        if not self.configured():
            self.emit("msg", "未配置 API key。请到设置页填写智谱 key（glm-4-flash 免费）。")
            return
        self._refresh_client()
        forced = _forced_platform(user_text)
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "system", "content": _dynamic_context()},
            *self.history,
            {"role": "user", "content": user_text},
        ]
        self.emit("msg", "思考中...")
        retried = False

        for turn in range(8):  # 最多 8 轮工具调用
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=TOOLS,
                    tool_choice="auto", max_tokens=2000,
                )
            except Exception as e:
                if not retried and self._retryable_api_error(e):
                    retried = True
                    self.emit("status", "模型请求失败，正在重试；不要重复道歉，直接继续未完成任务")
                    continue
                code = getattr(e, "status_code", None)
                if code == 401:
                    hint = "API Key 无效或过期，请在设置里重新填写"
                elif code == 429:
                    hint = "请求太频繁被限流，请稍后再试"
                elif code and 500 <= code < 600:
                    hint = "服务方临时故障，请稍后重试"
                else:
                    hint = f"调用模型失败：{str(e)[:160]}"
                self.emit("msg", hint)
                break
            retried = False
            choice = resp.choices[0]
            msg = choice.message

            if msg.content:
                self.emit("msg", msg.content)

            if not msg.tool_calls:
                break  # 模型认为任务完成

            # 助手消息只追加一次（含全部 tool_calls），否则多工具调用时重复入列会致 API 400
            messages.append(_message_dict(msg))
            for tc in msg.tool_calls:
                name = tc.function.name
                tool_call_id = getattr(tc, "id", "call_%s" % next(self._choice_seq))
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    if not isinstance(args, dict):
                        raise ValueError("参数不是对象")
                except Exception:
                    args = None
                result = self._execute_tool(name, args, forced, tool_call_id, user_text)
                self.emit("result", {**result, "name": name, "tool_call_id": tool_call_id})
                messages.append({
                    "role": "tool", "tool_call_id": tool_call_id,
                    "content": _result_str(result),
                })
        else:
            self.emit("msg", "（已执行多轮工具调用，如有未完成请再说一次）")

        self.history = _clip_history(messages[2:])
