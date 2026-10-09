"""宿主进程存活探测。

按产品规则：**账号在线严格参照进程**——托管该会话的进程（例如 DSH 进程）活着，
账号就在线；进程退出，账号就离线。

为什么用"进程"而不是"租约"：MCP 服务器是宿主拉起的子进程，它什么时候活着、什么时候
被回收，宿主的进程表就是最权威的事实。用租约去猜，只会猜错——DSH 会话在思考时不会
发心跳，租约一到就被判离线，于是"明明开着却发不进去"。

实现上只用标准库，Windows 与 POSIX 分别走各自的 API：

    POSIX   ``os.kill(pid, 0)``：进程存在则成功，不存在抛 ``ProcessLookupError``；
            但僵尸进程也会成功，所以再取一次进程状态，Z 视为已死。
    Windows ``OpenProcess`` + ``GetExitCodeProcess``：仍在使用（``STILL_ACTIVE``）
            才算活着；只查 PID 是否存在会把"已退出但句柄未回收"误判为在线。
"""

from __future__ import annotations

import os
import sys

__all__ = ["is_process_alive", "own_process_id", "describe_process", "process_started_at"]


def own_process_id() -> int:
    """当前进程 PID。"""
    return os.getpid()


def is_process_alive(pid: int | None) -> bool:
    """``pid`` 对应的进程是否仍然活着。

    保守策略：无法判断时返回 ``False``（宁可把账号判离线，也不要谎报在线——
    谎报在线会让发信方以为消息已送达，而它其实只是躺在队列里）。
    """
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _alive_windows(pid)
    return _alive_posix(pid)


def _alive_windows(pid: int) -> bool:
    """用 ``WaitForSingleObject`` 判定，**不要**用 ``GetExitCodeProcess == 259``。

    259（``STILL_ACTIVE``）同时是一个**合法退出码**：进程以 259 退出时，
    ``GetExitCodeProcess`` 会返回 259，于是"已经死掉的宿主"被永久判成在线
    （账号永远 connected，维护循环也不会回收它）。等待信号没有这个歧义：

        WAIT_TIMEOUT(258) -> 进程仍在运行
        WAIT_OBJECT_0(0)  -> 进程已结束（句柄已 signaled）
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x0
    WAIT_TIMEOUT = 0x102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
    )
    if not handle:
        # 打不开：要么进程不存在，要么我们没有权限。无权限时不能断定它死了，
        # 但按"宁可判离线"的策略返回 False。
        return False
    try:
        waited = kernel32.WaitForSingleObject(handle, 0)
        if waited == WAIT_TIMEOUT:
            return True
        if waited == WAIT_OBJECT_0:
            return False
        # 其它返回值（含 WAIT_FAILED）保守判离线。
        return False
    finally:
        kernel32.CloseHandle(handle)


def process_started_at(pid: int | None) -> float | None:
    """进程的创建时间（Unix 秒）；拿不到返回 ``None``。

    用途：把"托管进程"从"一个 PID 数字"变成"这个数字 + 它的创建时间"。
    只看 PID 存活无法区分"同一号进程"和"同号的另一个进程"（PID 复用），
    加上创建时间后，复用出来的新进程会被判成"不是原来的宿主" -> 离线。
    """
    if not pid or int(pid) <= 0:
        return None
    pid = int(pid)
    if sys.platform == "win32":
        return _started_at_windows(pid)
    return _started_at_posix(pid)


def _started_at_windows(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        # FILETIME 是 1601-01-01 起的 100ns；转 Unix 秒。
        return ticks / 10_000_000 - 11_644_473_600
    finally:
        kernel32.CloseHandle(handle)


def _started_at_posix(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[-1].split()
        # 第 22 个字段（去掉 comm 之后重新计数）是 starttime（时钟滴答）。
        start_ticks = int(fields[19])
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        with open("/proc/stat", "rb") as handle:
            for line in handle:
                if line.startswith(b"btime"):
                    boot = int(line.split()[1])
                    return boot + start_ticks / ticks_per_second
    except (OSError, IndexError, ValueError):
        return None
    return None


def _alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 存在但不是我们的进程：仍然算活着。
        return True
    except OSError:
        return False
    # 排除僵尸：进程已结束但父进程还没回收。
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[-1].split()
        if fields and fields[0:1] == [b"Z"]:
            return False
    except (OSError, IndexError):
        pass
    return True


def describe_process(pid: int | None) -> dict[str, object]:
    """给诊断用：进程 ID 与存活判定。"""
    return {"pid": pid, "alive": is_process_alive(pid)}
