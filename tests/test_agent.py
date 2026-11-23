"""Agent 工具派发与参数白名单测试。"""
import json
import os
from types import SimpleNamespace

import agent


def test_system_prompt_enforces_platform_routing():
    """回归：用户提到 B站时不能误选抖音工具。"""
    assert "提到 B站就绝不能调用任何 douyin_ 工具" in agent.SYSTEM
    assert "B站目前只支持按集下载" in agent.SYSTEM
    douyin_desc = next(t["function"]["description"] for t in agent.TOOLS
                       if t["function"]["name"] == "douyin_random")
    assert "B站" in douyin_desc and "不能用" in douyin_desc


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


def test_schema_excluded_argument_is_filtered(monkeypatch):
    """LLM 传入 Schema 未声明的 out_dir 必须被过滤，即使函数签名里有该参数。"""
    captured = {}

    def fake_download(url, out_dir=None):
        captured.update(url=url, out_dir=out_dir)
        return {"success": True}

    monkeypatch.setitem(agent.FUNCS, "url_download", fake_download)
    a, events = _make_agent(monkeypatch, [
        _completion([_tool_call("url_download", '{"url": "http://v", "out_dir": "/etc"}')]),
        _completion(),
    ])
    a.chat("下载")
    assert captured["url"] == "http://v"
    assert captured["out_dir"] is None, "Schema 外的 out_dir 被传入了工具函数"


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


def test_dynamic_context_has_no_secrets(monkeypatch):
    monkeypatch.setattr(agent.config, "llm_params", lambda: ("secret-key", "url", "model"))
    monkeypatch.setattr(agent.tools, "get_state", lambda: {"task": "", "done": 0,
                                                            "total": 0, "queue": []})
    monkeypatch.setattr(agent.tools, "get_login_status", lambda: {"bili": {"status": "ok"}})
    text = agent._dynamic_context()
    assert "secret-key" not in text
    assert "bili=ok" in text


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
