#!/usr/bin/env python3
"""Notify once when ChatMock's upstream weekly allowance is reset.

ChatMock updates ``usage_limits.json`` from the rate-limit headers returned by
ChatGPT.  A harmless models request refreshes those headers, so a systemd timer
can detect a reset without submitting an inference request or spending usage.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def read_snapshot() -> dict:
    """Read the exact weekly balance shown in Codex Cloud analytics.

    The request runs inside the ChatMock container because it already owns the
    refreshed ChatGPT OAuth session and its working egress route.  Its stdout is
    deliberately limited to the three non-secret values required by this timer.
    """
    code = r'''import json, requests
auth = json.load(open("/data/auth.json", encoding="utf-8"))["tokens"]
response = requests.get("https://chatgpt.com/backend-api/wham/usage", headers={
    "Authorization": "Bearer " + auth["access_token"],
    "chatgpt-account-id": auth["account_id"], "User-Agent": "Mozilla/5.0"}, timeout=30)
response.raise_for_status()
weekly = response.json()["rate_limit"]["primary_window"]
print(json.dumps({"used_percent": weekly["used_percent"], "reset_at": weekly["reset_at"]}))'''
    result = subprocess.run(
        ["/usr/bin/docker", "exec", "chatmock", "python", "-c", code],
        check=True, text=True, capture_output=True, timeout=45,
    )
    return json.loads(result.stdout)


def refresh_models(url: str) -> None:
    request = urllib.request.Request(url.rstrip("/") + "/v1/models", method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ChatMock refresh HTTP {response.status}")


def notify(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Telegram HTTP {response.status}")


def next_reset_text(snapshot: dict) -> str:
    moment = datetime.fromtimestamp(int(snapshot["reset_at"]), timezone.utc)
    return moment.astimezone().strftime("%d.%m.%Y, %H:%M %Z")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", type=Path, default=Path("/opt/studizba-platform/.state/chatmock-limit-monitor.json"))
    parser.add_argument("--chatmock-url", default="http://127.0.0.1:6666")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # This refreshes the OAuth session before querying the analytics endpoint.
    refresh_models(args.chatmock_url)
    current = read_snapshot()
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(args.state_file.read_text(encoding="utf-8")) if args.state_file.exists() else None

    # A reset changes the weekly window (usually from a non-zero value to near
    # zero) and moves its deadline forward.  The small tolerance avoids a false
    # alert from rounding in an upstream header.
    reset = bool(previous) and (
        current["used_percent"] + 1 < float(previous["used_percent"])
        and int(current["reset_at"]) > int(previous["reset_at"])
    )
    if reset:
        message = (
            "✅ Лимит ChatGPT/Codex сброшен.\n"
            f"Следующий сброс: {next_reset_text(current)}.\n"
            f"Доступно: {100 - current['used_percent']:.0f}%"
        )
        if not args.dry_run:
            notify(message)
        print("notified")
    else:
        print("unchanged")

    args.state_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
