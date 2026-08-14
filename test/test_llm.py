"""LLM 模块测试（不发起真实网络请求）"""
from pipeline import llm


def _cfg(**overrides):
    base = {
        "enabled": True,
        "api_url": "https://api.example.com/v1",
        "api_key": "sk-test-123",
        "model": "mimo-v2.5",
        "max_tokens": 1000,
        "temperature": 0.7,
        "timeout": 60,
    }
    base.update(overrides)
    return {"llm": base}


def test_is_enabled_default():
    assert llm.is_enabled(_cfg()) is True


def test_is_enabled_disabled():
    assert llm.is_enabled(_cfg(enabled=False)) is False


def test_is_enabled_missing_key():
    assert llm.is_enabled({"llm": {}}) is False


def test_is_enabled_placeholder():
    assert llm.is_enabled(_cfg(api_key="your-llm-api-key")) is False
    assert llm.is_enabled(_cfg(api_url="your-llm-api-url")) is False


def test_chat_disabled_returns_none():
    import asyncio
    assert asyncio.run(llm.chat(_cfg(enabled=False), "sys", "user")) is None


def test_chat_http_error_falls_back(monkeypatch):
    import asyncio

    class _FakeResp:
        status = 401

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return "unauthorized"

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            assert url == "https://api.example.com/v1/chat/completions"
            assert headers["Authorization"] == "Bearer sk-test-123"
            return _FakeResp()

    monkeypatch.setattr(llm.aiohttp, "ClientSession", lambda timeout=None: _FakeSession())
    assert asyncio.run(llm.chat(_cfg(), "sys", "user")) is None


def test_chat_success(monkeypatch):
    import asyncio

    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {"choices": [{"message": {"content": ' "a nice prompt" '}}]}

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            assert json["messages"][0]["role"] == "system"
            assert json["messages"][1]["content"] == "user-msg"
            return _FakeResp()

    monkeypatch.setattr(llm.aiohttp, "ClientSession", lambda timeout=None: _FakeSession())
    assert asyncio.run(llm.chat(_cfg(), "sys", "user-msg")) == "a nice prompt"


def test_url_appends_path_when_missing(monkeypatch):
    import asyncio

    seen = {}

    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            seen["url"] = url
            return _FakeResp()

    monkeypatch.setattr(llm.aiohttp, "ClientSession", lambda timeout=None: _FakeSession())
    asyncio.run(llm.chat(_cfg(api_url="https://api.example.com/v1/"), "s", "u"))
    assert seen["url"] == "https://api.example.com/v1/chat/completions"