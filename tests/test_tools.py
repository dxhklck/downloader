"""Cookie 转换与合并的纯函数测试。"""
import tools


def test_cookies_to_lines_netscape_format():
    lines = tools._cookies_to_lines([
        {"domain": ".example.com", "name": "a", "value": "1", "path": "/", "secure": True, "expires": 0},
        {"domain": "site.com", "name": "b", "value": "2", "path": "/x", "secure": False, "expires": -1},
    ])
    assert lines[0].split("\t") == [".example.com", "FALSE", "/", "TRUE", "0", "a", "1"]
    assert lines[1].split("\t") == ["site.com", "TRUE", "/x", "FALSE", "0", "b", "2"]


def test_merge_cookies_overwrites_same_domain_name(tmp_path, monkeypatch):
    path = tmp_path / "cookies.txt"
    path.write_text("# Netscape HTTP Cookie File\nold.com\tTRUE\t/\tFALSE\t0\tsid\told\nkeep.com\tTRUE\t/\tFALSE\t0\tk\tv\n", encoding="utf-8")
    monkeypatch.setattr(tools.config, "COOKIES_FILE", str(path))

    total = tools._merge_cookies_file(["old.com\tTRUE\t/\tFALSE\t0\tsid\tnew"])
    content = path.read_text(encoding="utf-8")
    assert "old.com\tTRUE\t/\tFALSE\t0\tsid\tnew" in content
    assert "sid\told" not in content
    assert "keep.com" in content
    assert total == 2


def test_site_login_rejects_bad_url():
    result = tools.site_login("ftp://example.com")
    assert result["success"] is False
    assert "http" in result["message"]
