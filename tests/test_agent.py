"""Agent 工具派发与参数白名单测试。"""
import json
import os
import threading
from types import SimpleNamespace

import agent


def test_system_prompt_enforces_platform_routing():
    """回归：用户提到 B站时不能误选抖音工具。"""
    assert "提到 B站就绝不能调用任何 douyin_ 工具" in agent.SYSTEM
    assert "按主题/关键词搜索收集视频用 search_videos" in agent.SYSTEM
    douyin_desc = next(t["function"]["description"] for t in agent.TOOLS
                       if t["function"]["name"] == "douyin_random")
    assert "B站" in douyin_desc and "不能用" in douyin_desc


def test_parse_intent_extracts_download_slots():
    intent = agent.parse_intent("B站搜信息科技互联网，下10个1080p到课程目录")
    assert intent["action"] == "search_download"
    assert intent["platform"] == "bili"
    assert "信息科技互联网" in intent["keyword"]
    assert intent["count"] == 10
    assert intent["quality"] == "1080"
    assert intent["out_dir"] == "课程"


def test_parse_intent_flags_missing_keyword():
    intent = agent.parse_intent("帮我收集一些视频")
    assert intent["action"] == "search_download"
    assert "keyword" in intent["missing"]


def test_forced_platform_detection():
    assert agent._forced_platform("B站随机下一首") == "bili_"
    assert agent._forced_platform("哔哩哔哩下载") == "bili_"
    assert agent._forced_platform("douyin 随机10个") == "douyin_"
    assert agent._forced_platform("随机下载20个搞笑视频") is None


def test_bilibili_request_blocks_douyin_tool(monkeypatch):
    """即便 LLM 选错工具，B站请求也绝不执行 douyin_random。"""
    called = []
    monkeypatch.setitem(agent.FUNCS, "douyin_random",
                        lambda **kw: called.append(kw) or {"success": True})
    a, events = _make_agent(monkeypatch, [
        _completion([_tool_call("douyin_random", '{"count": 1}')]),
        _completion(),
    ])
    a.chat("B站随机下一首")
    assert called == [], "B站请求错误地执行了抖音工具"
    results = [data for event, data in events if event == "result"]
    assert results[0]["success"] is False
    assert "B站" in results[0]["message"]


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _completion(tool_calls=None, content="done"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=content, tool_calls=tool_calls or []))]
    )


def _tool_call(name, arguments):
    tc = SimpleNamespace(id="call_1")
    tc.function = SimpleNamespace(name=name, arguments=arguments)
    return tc


def _make_agent(monkeypatch, responses):
    monkeypatch.setattr(agent, "OpenAI", lambda **kwargs: _FakeClient(responses))
    monkeypatch.setattr(agent, "config", SimpleNamespace(
        llm_params=lambda: ("key", "https://api.test", "model")))
    events = []
    a = agent.Agent(lambda event, data: events.append((event, data)))
    return a, events


def test_schema_declared_out_dir_is_passed(monkeypatch):
    """out_dir 已在 Schema 声明，应作为安全相对路径传给下载工具。"""
    captured = {}

    def fake_download(url, out_dir=None, quality="best"):
        captured.update(url=url, out_dir=out_dir, quality=quality)
        return {"success": True}

    monkeypatch.setitem(agent.FUNCS, "url_download", fake_download)
    a, events = _make_agent(monkeypatch, [
        _completion([_tool_call("url_download", '{"url": "http://v", "out_dir": "B站comfyui"}')]),
        _completion(),
    ])
    a.chat("下载")
    assert captured["url"] == "http://v"
    assert captured["out_dir"] == "B站comfyui"


def test_unknown_schema_argument_is_still_filtered(monkeypatch):
    """Schema 未声明的参数仍必须被过滤。"""
    captured = {}

    def fake_download(url, quality="best", out_dir=None, secret=None):
        captured.update(url=url, secret=secret)
        return {"success": True}

    monkeypatch.setitem(agent.FUNCS, "url_download", fake_download)
    a, events = _make_agent(monkeypatch, [
        _completion([_tool_call("url_download", '{"url": "http://v", "secret": "x"}')]),
        _completion(),
    ])
    a.chat("下载")
    assert captured["url"] == "http://v"
    assert captured["secret"] is None


def test_bili_link_allows_url_download(monkeypatch):
    """带视频链接时，B站不能硬拦截 url_download。"""
    called = []
    monkeypatch.setitem(agent.FUNCS, "url_download",
                        lambda url, quality="best", out_dir=None: called.append(
                            {"url": url, "quality": quality, "out_dir": out_dir}) or {"success": True})
    a, events = _make_agent(monkeypatch, [
        _completion([_tool_call("url_download", '{"url": "https://www.bilibili.com/video/BV1"}')]),
        _completion(),
    ])
    a.chat("下载链接 https://www.bilibili.com/video/BV1 中的视频")
    results = [data for event, data in events if event == "result"]
    assert results[0]["success"] is True
    assert called[0]["url"] == "https://www.bilibili.com/video/BV1"


def test_bili_no_link_still_blocks_douyin_tool(monkeypatch):
    """无链接的 B站随机请求仍不能走抖音工具。"""
    called = []
    monkeypatch.setitem(agent.FUNCS, "douyin_random",
                        lambda **kw: called.append(kw) or {"success": True})
    a, events = _make_agent(monkeypatch, [
        _completion([_tool_call("douyin_random", '{"count": 1}')]),
        _completion(),
    ])
    a.chat("B站随机下一首")
    assert called == []


def test_bili_keyword_search_allowed_without_link(monkeypatch):
    """用户说 B站 + 关键词时，search_videos 不应被平台硬路由拦截。"""
    called = []

    def fake_search(self, args):
        called.append(args.get("keyword"))
        self.emit("search_results", {"items": [], "keyword": "信息科技"})
        return {"success": True, "message": "ok", "data": {"items": [], "keyword": "信息科技"}}

    monkeypatch.setattr(agent.Agent, "_search_videos", fake_search)
    a, events = _make_agent(monkeypatch, [
        _completion([_tool_call("search_videos", '{"keyword": "信息科技", "platform": "bili"}')]),
        _completion(),
    ])
    a.chat("B站收集一些信息科技课程")
    assert called == ["信息科技"]
    events_of_search = [data for event, data in events if event == "search_results"]
    assert len(events_of_search) == 1


def test_unknown_tool_is_reported(monkeypatch):
    a, events = _make_agent(monkeypatch, [
        _completion([_tool_call("delete_all", "{}")]),
        _completion(),
    ])
    a.chat("hack")
    results = [data for event, data in events if event == "result"]
    assert results[0]["success"] is False
    assert "未知工具" in results[0]["message"]


def test_malformed_arguments_are_rejected(monkeypatch):
    a, events = _make_agent(monkeypatch, [
        _completion([_tool_call("get_state", "not-json")]),
        _completion(),
    ])
    a.chat("进度")
    results = [data for event, data in events if event == "result"]
    assert results[0]["success"] is False


def test_dynamic_context_includes_recent_results(monkeypatch):
    monkeypatch.setattr(agent.tools, "get_state", lambda: {
        "task": "", "done": 0, "total": 0, "queue": [],
        "recent_results": [{"success": True, "message": "已下载：测试视频"}],
    })
    monkeypatch.setattr(agent.tools, "get_login_status", lambda: {})
    text = agent._dynamic_context()
    assert "最近任务结果" in text
    assert "已下载：测试视频" in text


def test_make_summary_is_structured():
    summary = agent._make_summary([
        {"role": "user", "content": "找课程视频"},
        {"role": "tool", "content": '{"success": true, "message": "找到 3 个"}'},
        {"role": "assistant", "content": "已列出候选"},
    ])
    assert "用户诉求" in summary
    assert "工具结果" in summary
    assert "助手结论" in summary


def test_dynamic_context_has_no_secrets(monkeypatch):
    monkeypatch.setattr(agent.config, "llm_params", lambda: ("secret-key", "url", "model"))
    monkeypatch.setattr(agent.tools, "get_state", lambda: {"task": "", "done": 0,
                                                            "total": 0, "queue": []})
    monkeypatch.setattr(agent.tools, "get_login_status", lambda: {"bili": {"status": "ok"}})
    text = agent._dynamic_context()
    assert "secret-key" not in text
    assert "bili=ok" in text


def test_normalize_result_adds_next_action_for_candidate_lists():
    result = agent._normalize_result({"success": True, "message": "ok",
                                      "data": {"items": [{"title": "视频"}]}})
    assert "等待用户点选" in result["next_action"]


def test_clip_history_keeps_recent_messages():
    history = []
    for i in range(20):
        history.append({"role": "user", "content": "旧消息 %d" % i})
        history.append({"role": "assistant", "content": "回复 %d" % i})
    clipped = agent._clip_history(history)
    full_turns = [m for m in clipped if "此前对话摘要" not in str(m.get("content"))]
    assert "旧消息 0" not in " ".join(str(m["content"]) for m in full_turns)
    assert "旧消息 19" in " ".join(str(m["content"]) for m in full_turns)
    assert any("此前对话摘要" in str(m.get("content")) for m in clipped)


def test_large_tool_result_is_saved_with_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.config, "_APP_DIR", str(tmp_path))
    text = agent._result_str({"success": True, "message": "ok", "data": {"blob": "x" * 5000}})
    payload = json.loads(text)
    assert payload["data"]["truncated"] is True
    assert os.path.isfile(payload["data"]["file"])


def test_ask_choice_rejects_duplicate_answer(monkeypatch):
    a, _ = _make_agent(monkeypatch, [])
    entry = {"event": threading.Event(), "options": ["A", "B"], "answer": None}
    with a._choice_lock:
        a._choices["c1"] = entry
    first = a.answer_choice("c1", "A")
    second = a.answer_choice("c1", "B")
    assert first["ok"] is True
    assert second["ok"] is False
    assert entry["answer"] == "A"


def test_ask_choice_rejects_sensitive_question(monkeypatch):
    a, events = _make_agent(monkeypatch, [])
    result = a._ask_choice({"question": "请输入 API Key", "options": ["现已配置", "稍后再说"]})
    assert result["success"] is False
    assert result["error_code"] == "sensitive_choice"
    assert result["retryable"] is False
    assert not any(event == "ask_choice" for event, _ in events)


def test_tool_events_share_stable_fields(monkeypatch):
    a, events = _make_agent(monkeypatch, [
        _completion([_tool_call("get_state", "{}")]),
        _completion(),
    ])
    a.chat("进度")
    tool = next(data for event, data in events if event == "tool")
    done = next(data for event, data in events if event == "tool_done")
    for payload in (tool, done):
        assert {"name", "args", "status", "tool_call_id", "elapsed_ms"} <= set(payload)
    assert tool["tool_call_id"] == done["tool_call_id"]


def test_api_error_is_retried_once(monkeypatch):
    class FailOnce:
        def __init__(self):
            self.calls = 0

        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("timeout")
            return _completion(content="恢复成功")

    client = FailOnce()
    monkeypatch.setattr(agent, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(agent, "config", SimpleNamespace(
        llm_params=lambda: ("key", "https://api.test", "model")))
    events = []
    a = agent.Agent(lambda event, data: events.append((event, data)))
    a.chat("下载")
    assert client.calls == 2
    assert any(event == "msg" and data == "恢复成功" for event, data in events)
