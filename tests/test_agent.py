"""Agent 工具派发与参数白名单测试。"""
from types import SimpleNamespace

import agent


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
