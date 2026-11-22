"""配置读写测试：API Key 掩码与覆盖策略。"""
import json
import os

import pytest

import config
import main


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", str(path))
    return str(path)


def test_get_config_does_not_leak_api_key(cfg_file):
    config.save({"api_key": "sk-abcdefghijklmnopqrstuvwxyz", "configured_marker": 1})
    exposed = main.Api().get_config()
    assert "api_key" not in exposed
    assert exposed["configured"] is True


def test_save_config_blank_key_keeps_original(cfg_file):
    key = "sk-abcdefghijklmnopqrstuvwxyz"
    config.save({"api_key": key})
    result = main.Api().save_config(json.dumps({"api_key": "", "model": "glm-4-flash"}))
    assert result["ok"] is True
    assert config.load()["api_key"] == key
    assert config.load()["model"] == "glm-4-flash"


def test_save_config_explicit_key_overwrites(cfg_file):
    config.save({"api_key": "old-key"})
    result = main.Api().save_config(json.dumps({"api_key": "new-key-123"}))
    assert result["ok"] is True
    assert config.load()["api_key"] == "new-key-123"


def test_save_config_corrupted_file_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", str(path))
    assert config.load()["provider"] == "zhipu"


def test_export_import_ignore_sensitive_keys(cfg_file):
    config.save({"api_key": "secret", "model": "m1", "theme": "light"})
    exported = config.export_config()
    assert exported["ok"] is True
    assert "api_key" not in exported["config"]
    assert exported["config"]["theme"] == "light"

    result = config.import_config({"theme": "dark", "api_key": "hacked"})
    assert result["ok"] is True
    assert config.load()["api_key"] == "secret"
    assert config.load()["theme"] == "dark"
