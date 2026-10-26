"""Cookie handling for `moodlectl auth`: parse a MoodleSession out of whatever the user pastes or exports."""
from __future__ import annotations

import json
import re

COOKIE_NAME = "MoodleSession"


def extract_cookie_value(raw: str, cookie_name: str = COOKIE_NAME) -> str:
    """Accepts a bare value, `MoodleSession=...`, a raw Cookie header, a Netscape
    cookies.txt, or a browser JSON cookie export. Raises ValueError if nothing matches."""
    text = raw.strip()
    if not text:
        raise ValueError("no cookie content found")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    items: list = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("cookies") if isinstance(payload.get("cookies"), list) else [payload]
    for item in items:
        if isinstance(item, dict) and item.get("name") == cookie_name and item.get("value"):
            return str(item["value"]).strip()

    for line in text.splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 7 and parts[5] == cookie_name:  # Netscape cookies.txt
            return parts[6].strip()

    m = re.search(rf"(?:^|[;\s]){re.escape(cookie_name)}=([^;\s]+)", text)
    if m:
        return m.group(1).strip()

    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", text):
        return text
    raise ValueError(f"could not find {cookie_name} in the input")
