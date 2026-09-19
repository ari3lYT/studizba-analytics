from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GLMAccount:
    label: str
    api_key: str
    provider: str = "zai"
    model: str | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class GLMProxy:
    label: str
    url: str


@dataclass(frozen=True)
class Config:
    root: Path
    database_url: str
    studizba_username: str
    studizba_password: str
    glm_api_key: str
    glm_accounts: tuple[GLMAccount, ...]
    glm_proxies: tuple[GLMProxy, ...]
    glm_model: str
    glm_base_url: str
    base_url: str = "https://studizba.com"
    university_slug: str = "mgtu-im-baumana"
    university_source_id: int = 15
    request_delay: float = 0.75
    concurrency: int = 4
    analytics_half_life_days: float = 730.0
    glm_request_delay: float = 3.0

    @property
    def state_dir(self) -> Path:
        p = self.root / ".state"
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_config(path: str | Path | None = None, *, require_credentials: bool = False) -> Config:
    root = Path(os.getenv("STUDIZBA_ROOT", Path.cwd())).resolve()
    credentials_path = Path(path or os.getenv("STUDIZBA_CREDENTIALS", root / "credentials.local.json"))
    data: dict[str, Any] = {}
    if credentials_path.exists():
        data = json.loads(credentials_path.read_text(encoding="utf-8"))
    stud = data.get("studizba", {})
    glm = data.get("glm", {})
    username = os.getenv("STUDIZBA_USERNAME", stud.get("username", ""))
    password = os.getenv("STUDIZBA_PASSWORD", stud.get("password", ""))
    api_key = os.getenv("GLM_API_KEY", glm.get("api_key", ""))
    account_values = glm.get("accounts", [])
    accounts = tuple(
        GLMAccount(str(item.get("label", f"account-{i + 1}")), str(item["api_key"]),
                   str(item.get("provider", "zai")), item.get("model"), item.get("base_url"))
        for i, item in enumerate(account_values)
        if isinstance(item, dict) and item.get("api_key") and item.get("enabled", True)
    )
    proxies = tuple(
        GLMProxy(str(item.get("label", f"proxy-{i + 1}")), str(item["url"]))
        for i, item in enumerate(glm.get("proxies", []))
        if isinstance(item, dict) and item.get("url") and item.get("enabled", True)
    )
    # Legacy installations remain a one-provider pool.  An explicit environment
    # key overrides credentials for emergency single-key operation.
    if os.getenv("GLM_API_KEY"):
        accounts = (GLMAccount("environment", api_key),)
    elif not accounts and api_key:
        accounts = (GLMAccount("default", api_key),)
    gemini = data.get("gemini", {})
    gemini_key = os.getenv("GEMINI_API_KEY", gemini.get("api_key", ""))
    if gemini_key and gemini.get("enabled", True):
        accounts += (GLMAccount("gemini", gemini_key, "gemini", gemini.get("model", "gemini-3.6-flash")),)
    groq = data.get("groq", {})
    groq_key = os.getenv("GROQ_API_KEY", groq.get("api_key", ""))
    if groq_key and groq.get("enabled", True):
        accounts += (GLMAccount("groq", groq_key, "groq", groq.get("model", "qwen/qwen3.8-27b"),
                                "https://api.groq.com/openai/v1"),)
    openrouter = data.get("openrouter", {})
    openrouter_key = os.getenv("OPENROUTER_API_KEY", openrouter.get("api_key", ""))
    if openrouter_key and openrouter.get("enabled", True):
        accounts += (GLMAccount("openrouter", openrouter_key, "openrouter",
                                openrouter.get("model", "nvidia/nemotron-3.5-lightning:free"),
                                "https://openrouter.ai/api/v1"),)
    if require_credentials and (not username or not password):
        raise RuntimeError(f"Studizba credentials missing in {credentials_path}")
    return Config(
        root=root,
        database_url=os.getenv(
            "DATABASE_URL", "postgresql://studizba:studizba_local_only@127.0.0.1:54329/studizba"
        ),
        studizba_username=username,
        studizba_password=password,
        glm_api_key=api_key,
        glm_accounts=accounts,
        glm_proxies=proxies,
        glm_model=os.getenv("GLM_MODEL", glm.get("model", "glm-4.7-flash")),
        glm_base_url=os.getenv("GLM_BASE_URL", glm.get("base_url", "https://api.z.ai/api/paas/v4/")),
        request_delay=float(os.getenv("STUDIZBA_REQUEST_DELAY", "0.75")),
        concurrency=int(os.getenv("STUDIZBA_CONCURRENCY", "4")),
        analytics_half_life_days=float(os.getenv("ANALYTICS_HALF_LIFE_DAYS", "730")),
        glm_request_delay=float(os.getenv("GLM_REQUEST_DELAY", "3")),
    )
