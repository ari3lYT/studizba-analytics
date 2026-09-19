import json

from studizba.config import load_config


def test_disabled_providers_and_proxies_are_excluded(tmp_path, monkeypatch):
    for name in ("GLM_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({
        "glm": {
            "accounts": [
                {"label": "live", "api_key": "x"},
                {"label": "paused", "api_key": "y", "enabled": False},
            ],
            "proxies": [
                {"label": "live-route", "url": "http://127.0.0.1:1"},
                {"label": "dead-route", "url": "http://127.0.0.1:2", "enabled": False},
            ],
        },
        "gemini": {"api_key": "g", "enabled": False},
        "groq": {"api_key": "q", "enabled": False},
        "openrouter": {"api_key": "o", "enabled": False},
    }), encoding="utf-8")

    config = load_config(credentials)

    assert [account.label for account in config.glm_accounts] == ["live"]
    assert [proxy.label for proxy in config.glm_proxies] == ["live-route"]
