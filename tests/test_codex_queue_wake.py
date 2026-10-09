from mcp_agent_mailbox.adapters.base import CommandResult
from mcp_agent_mailbox.adapters.codex_queue_wake import CodexQueueWaker


class Runner:
    def __init__(self, output="Queued message msg-1 for thread thread-1.", rc=0):
        self.output, self.rc, self.calls = output, rc, []

    def run(self, argv, timeout):
        self.calls.append(argv)
        if argv[-1] == "--help":
            return CommandResult(tuple(argv), 0, "--thread --message", "")
        return CommandResult(tuple(argv), self.rc, self.output, "")


def test_queue_targets_exact_thread_without_permission_overrides():
    runner = Runner()
    waker = CodexQueueWaker("codex.exe", runner=runner)
    assert waker.wake("thread-1", "notice").started
    assert runner.calls[-1] == ["codex.exe", "queue", "--thread", "thread-1", "--message", "notice"]


def test_success_exit_without_target_receipt_is_not_delivery():
    assert not CodexQueueWaker("codex.exe", runner=Runner(output="OK")).wake("thread-1", "notice").started


def test_wrong_target_is_not_delivery():
    assert not CodexQueueWaker("codex.exe", runner=Runner()).wake("thread-2", "notice").started


def test_failed_queue_never_falls_back_to_exec_resume():
    runner = Runner(rc=1)
    assert not CodexQueueWaker("codex.exe", runner=runner).wake("thread-1", "notice").started
    assert len(runner.calls) == 2
