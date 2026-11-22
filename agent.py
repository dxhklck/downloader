"""LLM agent：OpenAI 兼容 function calling 循环。

把用户的自然语言理解成工具调用，执行后把结果回灌模型，直到任务完成。
API key 从 config.json 读取，不硬编码。
"""
import json, threading, inspect
from openai import OpenAI

import config
import tools


def _result_str(result, limit=1500):
    """工具结果回灌前截断，防止长日志/大列表吃满 token。"""
    try:
        s = json.dumps(result, ensure_ascii=False)
    except Exception:
        s = str(result)
    if len(s) > limit:
        # 保留结构，重点砍掉 data.logs 之类的长文本
        trimmed = dict(result) if isinstance(result, dict) else {"raw": result}
        if isinstance(trimmed.get("data"), dict):
            d = dict(trimmed["data"])
            for k in ("logs", "log", "ranges"):
                if k in d and isinstance(d[k], list) and len(d[k]) > 20:
                    d[k] = d[k][:20] + [f"...(共{len(trimmed['data'][k])}项,已截断)"]
            trimmed["data"] = d
        try:
            s = json.dumps(trimmed, ensure_ascii=False)
        except Exception:
            pass
        if len(s) > limit:
            s = s[:limit] + "…(已截断)"
    return s

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
            "description": "按视频网页链接下载任意网站的视频。用户给出 http:// 或 https:// 链接（如 YouTube、B站、抖音、微博等）时用这个工具，不要用 douyin_random。参数只有一个：完整的视频链接 url。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整的视频网页链接，以 http:// 或 https:// 开头"},
                },
                "required": ["url"],
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
}

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


def _forced_platform(text):
    """从用户原话识别明确平台，返回允许的工具前缀；未明确则返回 None。"""
    low = (text or "").lower()
    for words, prefix in _PLATFORM_PREFIXES:
        if any(w in low for w in words):
            return prefix
    return None

SYSTEM = """你是视频下载助手。用户用中文告诉你要下载什么，你通过调用工具完成任务。
最重要的规则：先看用户提到的平台（B站/哔哩哔哩/bilibili 或 抖音/douyin），再选工具。
平台是硬约束：**提到 B站就绝不能调用任何 douyin_ 工具，提到抖音就绝不能调用任何 bili_ 工具。**
没有明确平台时才按下面的默认规则处理。

平台识别：
- 提到 B站/哔哩哔哩/bilibili → 只能用 bili_ 工具（bili_download / bili_status）。B站目前只支持按集下载番剧，不支持随机/音乐/关键词下载；若用户要求"B站随机""B站下一首""B站唱歌"等，直接文字回复"B站目前只支持按集下载番剧，不支持随机或音乐筛选；请告诉我具体集数范围，或发视频链接用链接下载"，不要调用任何工具。
- 提到抖音/douyin → 只能用 douyin_ 工具（douyin_random / douyin_login）。
- 两个平台都没提 → 按默认规则（下面第 3、4 条）处理。

默认规则：
1. 用户给出 http:// 或 https:// 开头的网页链接（YouTube、B站、抖音、微博等任意网站）→ 一律调 url_download，把完整链接原样传给 url 参数。
2. 用户说下载番剧/集数（没给链接）→ 调 bili_download；集数范围从用户的话里提取。若只说剧名没给范围，先调 bili_status 查总集数再决定，不要臆测数字。
3. 用户说随机刷/刷到什么下什么（没给链接，且没提 B站）→ 调 douyin_random，数量从话里提取。
4. 用户说唱歌/音乐类（没给链接，且没提 B站）→ 调 douyin_random 并传 keywords=['唱歌','翻唱','现场','唱']。
5. 用户问当前任务进度/下完没 → 调 get_state；问某部番剧总共下了多少集 → 调 bili_status。
6. 用户说登录 → 调 douyin_login；但提到 B站登录时，文字回复"B站请在设置页通用登录里打开 bilibili.com 扫码"，不要调 douyin_login。
7. 用户说清理 → 调 clean_temp。
8. 每次只返回一个工具调用，等结果后再决定下一步。任务完成后用简洁中文汇报结果。
9. 如果工具返回失败（success=false），如实告诉用户原因，不要假装成功。
"""


class Agent:
    def __init__(self, emit):
        """emit(event, data) 用于向界面推送消息/工具调用。"""
        self.emit = emit
        self.client = None
        self.model = None
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

    def chat(self, user_text):
        """处理一条用户消息。在后台线程调用，通过 emit 推送过程。"""
        if not self.configured():
            self.emit("msg", "未配置 API key。请到设置页填写智谱 key（glm-4-flash 免费）。")
            return
        self._refresh_client()
        forced = _forced_platform(user_text)
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_text},
        ]
        self.emit("msg", "思考中...")

        for turn in range(8):  # 最多 8 轮工具调用
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=TOOLS,
                    tool_choice="auto", max_tokens=2000,
                )
            except Exception as e:
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
                return
            choice = resp.choices[0]
            msg = choice.message

            if msg.content:
                self.emit("msg", msg.content)

            if not msg.tool_calls:
                return  # 模型认为任务完成

            # 助手消息只追加一次（含全部 tool_calls），否则多工具调用时重复入列会致 API 400
            messages.append(msg)
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    if not isinstance(args, dict):
                        raise ValueError("参数不是对象")
                except Exception:
                    args = None
                self.emit("tool", {"name": name, "args": args or {}})
                fn = FUNCS.get(name)
                if forced and not name.startswith(forced):
                    platform = "B站" if forced == "bili_" else "抖音"
                    result = {"success": False,
                              "message": f"用户明确指定{platform}，禁止调用 {name}。请改用{platform}的工具，或文字说明该平台不支持此需求。"}
                elif args is None:
                    result = {"success": False, "message": "工具参数格式错误，请重新调用"}
                elif not fn:
                    result = {"success": False, "message": f"未知工具 {name}"}
                else:
                    # 双重白名单：Schema 未声明的参数一律丢弃（防越权），
                    # 且函数签名不接受的参数也不传（防 TypeError）。
                    schema_keys = _SCHEMA_PARAMS.get(name, set())
                    signature_keys = set(inspect.signature(fn).parameters)
                    accepted = schema_keys & signature_keys
                    call_args = {k: v for k, v in args.items() if k in accepted}
                    try:
                        result = fn(**call_args)
                    except Exception as e:
                        result = {"success": False, "message": str(e)[:200]}
                self.emit("result", result)
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": _result_str(result),
                })

        self.emit("msg", "（已执行多轮工具调用，如有未完成请再说一次）")
