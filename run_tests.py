# 一次跑完两套隔离测试：uv run python run_tests.py
#
# 设计给 CI 和本地共用。某些受限环境（沙箱/受控桌面）不允许在系统临时目录下
# 创建嵌套目录，这里把临时根目录固定到仓库内，并在被拒时自动回退，
# 保证测试在任何环境下都能跑完，且不写真实 ~/.board-mcp 数据。
import itertools
import os
import runpy
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRATCH = ROOT / '.test-scratch'
TESTS = ('test_demo.py', 'test_upgrade.py')


def reset_scratch() -> None:
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)


def install_temp_shim() -> None:
    """把临时目录钉在仓库内，避开受限环境下嵌套目录创建被拒的问题。

    注意：临时目录落在仓库树里时，git 会向上冒泡找到本仓库的 .git，
    于是"非 git 目录"用例会误认成当前仓库。GIT_CEILING_DIRECTORIES 把
    git 的向上查找截止在 .test-scratch，让隔离和系统临时目录一致。
    另外建一个空的 .git 目录，保证 git 在这里判定"不是仓库"。
    """
    os.environ['GIT_CEILING_DIRECTORIES'] = str(SCRATCH)
    (SCRATCH / '.git').mkdir(exist_ok=True)

    real_mkdir = os.mkdir
    counter = itertools.count(1)

    def mkdtemp(suffix=None, prefix=None, dir=None):
        base = Path(dir) if dir else SCRATCH
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


def run(test: str) -> bool:
    print(f'===== {test} =====', flush=True)
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
        reset_scratch()
    return True


def main() -> int:
    reset_scratch()
    install_temp_shim()
    ok = all(run(t) for t in TESTS)
    print('\n全部通过' if ok else '\n存在失败', flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
