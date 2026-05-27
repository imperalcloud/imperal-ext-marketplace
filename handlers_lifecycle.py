"""Marketplace · install / uninstall handlers.

Split from handlers.py per the federal handlers-<300 LOC rule (2026-05-27).
Federal confirmation gate auto-fires on action_type='write' and
action_type='destructive' before dispatch. Both handlers also invalidate
kernel-side caches so the Hub picks up the install/uninstall immediately.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat import ActionResult

from api import _invalidate_kernel_caches, post_install_app, post_uninstall_app
from app import chat
from models import AppIdParams, InstallResult, UninstallResult

log = logging.getLogger("marketplace.handlers_lifecycle")


# ─── Write / Destructive (federal confirmation gate auto-fires) ───────── #

@chat.function(
    "install_app",
    action_type="write",
    effects=["create:install"],
    id_projection="app_id",
    event="marketplace.app_installed",
    data_model=InstallResult,
    description=(
        "Install a Marketplace app for the current user. Use when user "
        "says 'install X', 'add X', 'I want X' OR when they accept a "
        "prior offer with anaphoric reference like 'yes', 'go ahead', "
        "'install it', 'do it'. "
        "REQUIRED arg: app_id (string). When the user message uses an "
        "anaphoric pronoun ('it', 'that one'), resolve app_id from the "
        "MOST RECENT app mentioned in conversation — typically the "
        "subject of the prior search_marketplace / get_app_details / "
        "recommend_for_intent result, OR from the previous assistant "
        "offer ('Should I install spotify?' → app_id='spotify'). "
        "Confirmation gate fires automatically; user must confirm."
    ),
)
async def fn_install_app(ctx, params: AppIdParams) -> ActionResult:
    """Install the named Marketplace app for the current user.

    Federal V10 event 'marketplace.app_installed' fires after a
    successful install so subscribers (skeletons, automation rules,
    audit ledger) can react.
    """
    try:
        result = await post_install_app(ctx, params.app_id)
    except Exception as exc:
        log.warning("install_app %s: %s", params.app_id, exc, exc_info=True)
        return ActionResult.error(
            f"Failed to install '{params.app_id}': {str(exc)[:200]}"
        )

    # Force Hub/sidebar/classifier to see the new install immediately —
    # kernel-side accessible_* caches + skeleton workflow reload.
    await _invalidate_kernel_caches(ctx, params.app_id)

    return ActionResult.success(
        data=result,
        summary=(
            f"Installed '{params.app_id}' for you. "
            f"Total installs: {result.get('install_count', '?')}."
        ),
    )


@chat.function(
    "uninstall_app",
    action_type="destructive",
    effects=["delete:install"],
    id_projection="app_id",
    event="marketplace.app_uninstalled",
    data_model=UninstallResult,
    description=(
        "Uninstall a Marketplace app from the current user's account. "
        "Use when user says 'uninstall X', 'remove X', 'I don't need X' "
        "OR when they confirm a prior offer with anaphoric reference like "
        "'yes', 'uninstall it', 'remove it', 'go ahead', 'do it'. "
        "REQUIRED arg: app_id (string). For anaphoric references "
        "('it', 'that one'), resolve app_id from the MOST RECENT app "
        "mentioned in conversation — typically the just-installed app, "
        "the subject of the prior search_marketplace, OR the app named "
        "in the assistant's previous offer ('Should I uninstall spotify?' "
        "→ app_id='spotify'). NEVER request clarification when the prior "
        "turn unambiguously named the app; just dispatch with that "
        "app_id. Confirmation gate fires automatically; user must "
        "confirm before destructive action runs."
    ),
)
async def fn_uninstall_app(ctx, params: AppIdParams) -> ActionResult:
    """Uninstall the named Marketplace app for the current user.

    Belt-and-suspenders cache invalidation: auth-gw service.uninstall_app
    already calls _clear_user_skeleton_state; the extension-side duplicate
    here is harmless and guarantees Hub picks up the change even if the
    auth-gw cascade was interrupted.
    """
    try:
        result = await post_uninstall_app(ctx, params.app_id)
    except Exception as exc:
        log.warning("uninstall_app %s: %s", params.app_id, exc, exc_info=True)
        return ActionResult.error(
            f"Failed to uninstall '{params.app_id}': {str(exc)[:200]}"
        )

    await _invalidate_kernel_caches(ctx, params.app_id)

    return ActionResult.success(
        data=result,
        summary=(
            f"Uninstalled '{params.app_id}'. "
            f"Active installs remaining for that app: "
            f"{result.get('install_count', '?')}."
        ),
    )
