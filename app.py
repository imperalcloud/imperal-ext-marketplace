"""Marketplace · Extension declaration.

Imperal-owned, system=1 (always accessible to every user without explicit
install). Bridges chat to the auth-gw /v1/marketplace/* HTTP surface so
Webbee can discover, recommend, install, uninstall and review apps from
within the chat.
"""
from __future__ import annotations

import logging
from pathlib import Path

from imperal_sdk import Extension
from imperal_sdk.chat import ChatExtension

log = logging.getLogger("marketplace")

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.txt").read_text()


ext = Extension(
    "marketplace",
    version="1.0.0",
    capabilities=[
        "marketplace:read",
        "marketplace:write",
        "store:read",
        "store:write",
        "ai:complete",
        "marketplace:*",
    ],
    display_name="Marketplace",
    description=(
        "Browse, discover and install apps in Imperal Marketplace from chat. "
        "Webbee can recommend apps matching what you need, show details and "
        "reviews, install or remove apps with confirmation."
    ),
    icon="icon.svg",
    actions_explicit=True,
)


chat = ChatExtension(
    ext,
    "tool_marketplace_chat",
    description=(
        "Marketplace manager — discover, install, review apps. "
        "Use when the user wants to find/install/remove/review an app, or "
        "when their request hints they need a capability the platform may "
        "have as a Marketplace app."
    ),
    system_prompt=SYSTEM_PROMPT,
)


@ext.health_check
async def health(ctx) -> dict:
    return {"status": "ok", "version": ext.version}
