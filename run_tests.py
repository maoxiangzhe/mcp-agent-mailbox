# 一次跑完全部测试：uv run python run_tests.py
#
# 两套测试，一次跑完：
#   1. 新系统（会话寻址邮箱）：pytest 收集 tests/，含单元、集成、契约与真实 stdio 往返；
#   2. 旧版兼容回归：test_demo.py / test_upgrade.py（公告板 + 旧收件箱）。
#
# 设计给 CI 和本地共用。受限环境（沙箱/受控桌面）不允许在系统临时目录下创建嵌套
# 目录，因此这里把临时根目录钉在仓库内：
#   - pytest 通过 pyproject 的 --basetemp 落在 .test-scratch/pytest；
#   - 旧脚本通过下面的 tempfile / os.mkdir 垫片落在 .test-scratch。
# 同时用 GIT_CEILING_DIRECTORIES 阻止 git 向上冒泡，让"非 git 目录"用例保持隔离，
# 并保证测试不写任何真实用户目录（~/.board-mcp、~/.dsh、~/.codex）。
import itertools
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRATCH = ROOT / '.test-scratch'
LEGACY_TESTS = ('test_demo.py', 'test_upgrade.py')


def scratch_candidates() -> list[Path]:
    """临时根的候选顺序：显式覆盖 -> 仓库内 -> 逐级向上的祖先目录。

    沙箱可能把仓库目录的权限项改坏到**连子目录都建不了**（文件能写、目录不能），
    那就逐级向上找第一个能建目录的祖先——测试不该被这种事卡死。
    """
    candidates: list[Path] = []
    override = os.environ.get('MAILBOX_TEST_SCRATCH', '').strip()
    if override:
        candidates.append(Path(override))
    candidates.append(SCRATCH)
    candidates.extend(ROOT / f'.test-scratch-{i}' for i in range(2, 6))
    candidates.extend(parent / '.mailbox-test-scratch' for parent in ROOT.parents)
    return candidates


def pick_scratch() -> Path:
    """挑一个当前进程真的写得进去的临时根（能建文件、也能建/删子目录）。"""
    for candidate in scratch_candidates():
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            (candidate / '.git').mkdir(exist_ok=True)
            # 用带 PID 的唯一名字，并且**不用 exist_ok**：必须真的建出来又删得掉。
            # 用固定名字会踩到上次留下的同名目录，mkdir(exist_ok=True) 变成空操作，
            # 于是"探测通过"而实际建不了新目录（临时目录全挂，报 No usable temporary directory）。
            probe_dir = candidate / f'.probe-{os.getpid()}'
            probe_dir.mkdir()
            (probe_dir / 'probe.txt').write_text('ok', encoding='utf-8')
            probe_dir.rmdir()
            return candidate
        except OSError:
            continue
    return SCRATCH


def reset_scratch(scratch: Path) -> None:
    # 只清我们自己建过的子目录：根目录可能被沙箱 ACL 锁住，删不掉也不该致命。
    shutil.rmtree(scratch / 'pytest', ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)


def install_temp_shim(scratch: Path) -> None:
    """把临时目录钉在仓库内，避开受限环境下嵌套目录创建被拒的问题。

    注意：临时目录落在仓库树里时，git 会向上冒泡找到本仓库的 .git，
    于是"非 git 目录"用例会误认成当前仓库。GIT_CEILING_DIRECTORIES 把
    git 的向上查找截止在 .test-scratch，让隔离和系统临时目录一致。
    另外建一个空的 .git 目录，保证 git 在这里判定"不是仓库"。
    """
    os.environ['GIT_CEILING_DIRECTORIES'] = str(scratch)
    try:
        (scratch / '.git').mkdir(exist_ok=True)
    except OSError:
        # pick_scratch 已经验过这一步能成功；真失败也不该让整套测试崩在这里。
        pass

    real_mkdir = os.mkdir
    counter = itertools.count(1)

    def mkdtemp(suffix=None, prefix=None, dir=None):
        base = Path(dir) if dir else scratch
        for i in itertools.count():
            candidate = base / f'{prefix or "tmp"}{i}{suffix or ""}'
            try:
                real_mkdir(candidate)
            except FileExistsError:
                continue
            return str(candidate)
        raise RuntimeError('unreachable')

    def mkdir(path, mode=0o777, *args, **kwargs):
        try:
            return real_mkdir(path, mode, *args, **kwargs)
        except PermissionError:
            if not Path(path).is_absolute():
                raise
            return real_mkdir(SCRATCH / f'appdir-{next(counter)}', mode, *args, **kwargs)

    os.mkdir = mkdir
    tempfile.mkdtemp = mkdtemp
    os.environ['TEMP'] = str(SCRATCH)
    os.environ['TMP'] = str(SCRATCH)


def run_pytest(scratch: Path) -> bool:
    """跑新系统测试（单元 + 集成 + 契约 + stdio 往返）。"""
    print('===== pytest tests/ （会话寻址邮箱）=====', flush=True)
    # 每次用**唯一**的 basetemp：旧目录可能被沙箱 ACL 锁住、连删都删不掉，
    # 而 pytest 启动时会把已存在的 basetemp 清空——那会直接让整套测试报错。
    basetemp = scratch / f'pytest-{os.getpid()}'
    result = subprocess.run(
        [
            sys.executable,
            '-B',
            '-m',
            'pytest',
            str(ROOT / 'tests'),
            f'--basetemp={basetemp}',
        ],
        cwd=str(ROOT),
    )
    return result.returncode == 0


def run_legacy(test: str, scratch: Path) -> bool:
    """跑旧版兼容回归。"""
    print(f'===== {test} （旧版兼容）=====', flush=True)
    saved = sys.argv[:]
    sys.argv = [str(ROOT / test)]
    try:
        runpy.run_path(str(ROOT / test), run_name='__main__')
    except SystemExit as exc:
        if exc.code not in (0, None):
            print(f'[{test}] 退出码 {exc.code}', flush=True)
            return False
    finally:
        sys.argv = saved
        reset_scratch(scratch)
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    skip_legacy = '--no-legacy' in args
    scratch = pick_scratch()
    reset_scratch(scratch)
    install_temp_shim(scratch)

    results: list[tuple[str, bool]] = [('pytest tests/', run_pytest(scratch))]
    if not skip_legacy:
        for test in LEGACY_TESTS:
            results.append((test, run_legacy(test, scratch)))

    print('\n===== 汇总 =====', flush=True)
    for name, ok in results:
        print(f'  [{"PASS" if ok else "FAIL"}] {name}', flush=True)
    ok = all(result for _, result in results)
    print('\n全部通过' if ok else '\n存在失败', flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
