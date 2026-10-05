"""Serialized single-message Telegram UI.

Every chat owns one canonical bot message.  Network/rate-limit errors never
create duplicates; replacement is allowed only when Telegram explicitly says
that the old message is gone or cannot be edited.
"""

from __future__ import annotations

from .state import (
    ScreenContent,
    ScreenLoader,
    RichPhotoScreen,
    RichScreenBody,
    RichScreenContent,
    RichScreenLoader,
    callback_action,
    restore_screen_targets,
    current_screen_token,
    is_current_screen,
)
from .screens import (
    stop_live_updates,
    render_callback_screen,
    render_if_current,
    render_rich_if_current,
    render_command_screen,
    start_live_updates,
    render_live_screen,
    start_rich_live_updates,
    render_rich_live_screen,
)
from .events import (
    register_bot,
    unregister_bot,
    refresh_restored_screens,
    publish_event,
    publish_event_to_chat,
    deliver_event_to_chat,
)
from .middleware import (
    EventBannerSnapshotMiddleware,
    CancelLiveUpdatesMiddleware,
)

__all__ = [
    "ScreenContent",
    "ScreenLoader",
    "RichPhotoScreen",
    "RichScreenBody",
    "RichScreenContent",
    "RichScreenLoader",
    "callback_action",
    "restore_screen_targets",
    "current_screen_token",
    "is_current_screen",
    "stop_live_updates",
    "render_callback_screen",
    "render_if_current",
    "render_rich_if_current",
    "render_command_screen",
    "start_live_updates",
    "render_live_screen",
    "start_rich_live_updates",
    "render_rich_live_screen",
    "register_bot",
    "unregister_bot",
    "refresh_restored_screens",
    "publish_event",
    "publish_event_to_chat",
    "deliver_event_to_chat",
    "EventBannerSnapshotMiddleware",
    "CancelLiveUpdatesMiddleware",
]
