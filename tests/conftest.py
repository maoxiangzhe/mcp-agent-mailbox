"""测试夹具。

隔离要求（任务与设计文档 §15）：

- 测试**绝不**写入真实的 ``~/.board-mcp``、``~/.dsh``、``~/.codex`` 或任何真实邮箱
  数据库；每个测试一个 ``tmp_path``；
- 临时根目录固定在仓库内的 ``.test-scratch``：受限环境（沙箱/受控桌面）不允许在
  系统临时目录下创建嵌套目录，固定到仓库内才能稳定跑通；
- 该目录下放一个空 ``.git``，并设置 ``GIT_CEILING_DIRECTORIES``，让"非 git 目录"
  这类用例不会向上冒泡误认成本仓库。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRATCH = REPO_ROOT / ".test-scratch"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _scratch_candidates() -> list[Path]:
    """临时根的候选顺序：显式覆盖 -> 仓库内候选。

    "仓库内的 .test-scratch"是首选（受限环境不允许在系统临时目录建嵌套目录）；
    但沙箱可能把仓库目录的权限项改坏到**连子目录都建不了**（文件能写、目录不能），
    那就尝试其他仓库内目录；不能往工作区根或祖先目录散落测试文件。
    """
    candidates: list[Path] = []
    override = os.environ.get("MAILBOX_TEST_SCRATCH", "").strip()
    if override:
        candidates.append(Path(override))
    candidates.append(SCRATCH)
    candidates.extend(REPO_ROOT / f".test-scratch-{index}" for index in range(2, 6))
    return candidates


def _pick_writable_scratch() -> Path:
    """挑一个**当前进程真的写得进去**的临时根。"""
    for candidate in _scratch_candidates():
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            (candidate / ".git").mkdir(exist_ok=True)
            # 带 PID 的唯一名字 + 不用 exist_ok：必须真的建出来又删得掉。固定名字会踩到
            # 上次留下的同名目录，mkdir(exist_ok=True) 变成空操作，于是"探测通过"而实际
            # 建不了新目录（临时目录全挂，pytest 报 No usable temporary directory）。
            probe_dir = candidate / f".probe-{os.getpid()}"
            probe_dir.mkdir()
            (probe_dir / "probe.txt").write_text("ok", encoding="utf-8")
            (probe_dir / "probe.txt").unlink()
            probe_dir.rmdir()
            return candidate
        except OSError:
            continue
    return SCRATCH  # 都不行就照旧用默认路径，让失败如实暴露


def _install_scratch_root() -> None:
    """把临时目录钉在仓库内，并阻止 git 向上冒泡。

    ``GIT_CEILING_DIRECTORIES`` 让"非 git 目录"这类用例不会向上找到本仓库的
    ``.git``；同时放一个空的 ``.git`` 目录，保证 git 在这里判定"不是仓库"。
    """
    global SCRATCH
    SCRATCH = _pick_writable_scratch()
    os.environ.setdefault("GIT_CEILING_DIRECTORIES", str(SCRATCH))
    os.environ["TEMP"] = str(SCRATCH)
    os.environ["TMP"] = str(SCRATCH)
    # 测试**绝不能**装配真实的宿主注入通道：Broker 会按 MAILBOX_DSH_HOME/DSH_HOME
    # 找 DSH 的签名凭据，一旦找到就真的会 POST /api/session/prompt（在有凭据的机器上
    # 会把测试消息注入真实会话，断言也会随环境漂移）。这里钉到一个不存在的目录。
    os.environ["MAILBOX_DSH_HOME"] = str(SCRATCH / "no-such-dsh-home")
    # 同理：通道B（DSH 内插件）也不能在测试里被自动装配。
    os.environ.pop("MAILBOX_DSH_WAKE_TOKEN", None)
    os.environ.pop("MAILBOX_DSH_WAKE_URL", None)


_install_scratch_root()


class UnitOfWorkFactory:
    """测试用服务容器：把数据库包成"可开事务 + 直接读"的入口。

    生产代码里对应 ``daemon.Broker``；测试里只关心事务与仓储，所以这里给一个
    最小实现，避免测试依赖完整 Broker。
    """

    def __init__(self, database) -> None:
        self.database = database

    def transaction(self, **kwargs):
        from mcp_agent_mailbox.infrastructure.sqlite import SqliteUnitOfWork

        return SqliteUnitOfWork(self.database).transaction()

    def unit_of_work(self):
        from mcp_agent_mailbox.infrastructure.sqlite import SqliteUnitOfWork

        return SqliteUnitOfWork(self.database)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """一个干净的仓库内临时目录。"""
    return tmp_path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """一个未初始化的数据库路径。"""
    return tmp_path / "mailbox.sqlite3"


@pytest.fixture
def database(db_path: Path):
    """已迁移到最新版本的数据库。"""
    from mcp_agent_mailbox.infrastructure.sqlite import initialize

    opened = initialize(db_path)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def uow(database):
    """事务入口（测试辅助容器）。"""
    return UnitOfWorkFactory(database)
