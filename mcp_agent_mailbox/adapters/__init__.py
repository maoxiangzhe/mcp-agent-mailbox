"""宿主适配器层。

每个宿主一个适配器，遵循 ``ports.host_adapter.HostAdapter`` 的统一契约。
能力等级必须如实声明：``verified=False`` 表示"已实现但未在真实宿主上端到端验证"，
文档与 ``whoami`` 都要照此表述。
"""

from __future__ import annotations

from .base import (
    BaseAdapter,
    CommandResult,
    CommandRunner,
    SubprocessRunner,
    adapter_registry,
    capability_matrix,
    envelope_from_payload,
)
from .codex import CodexAdapter, CodexSession, parse_session_listing, probe_codex
from .dsh import DshAdapter, level2_guidance, probe_dsh

__all__ = [
    "BaseAdapter",
    "CodexAdapter",
    "CodexSession",
    "CommandResult",
    "CommandRunner",
    "DshAdapter",
    "SubprocessRunner",
    "adapter_registry",
    "capability_matrix",
    "envelope_from_payload",
    "level2_guidance",
    "parse_session_listing",
    "probe_codex",
    "probe_dsh",
]
