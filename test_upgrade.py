"""Independent upgrade regression tests; never open the real board data."""
import os
import tempfile
import unittest
import asyncio
import sys
import json
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_sandbox = tempfile.TemporaryDirectory(prefix='board-upgrade-')
os.environ['BOARD_MCP_ROOT'] = _sandbox.name
import server


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.project = self.id().replace('.', '-')

    def send(self, text='task', **kw):
        return server.send_note('author', 'reader', text, project=self.project, **kw)

    def notes(self):
        return server._read_notes(server._resolve_project(self.project)[0])

    def test_pagination_does_not_hide_unread(self):
        for i in range(7): self.send(str(i))
        first = server.read_notes('reader', limit=3, project=self.project)
        self.assertIn('#1 ', first)
        self.assertNotIn('#7 ', first)
        second = server.read_notes('reader', limit=3, project=self.project)
        self.assertIn('#4 ', second)
        third = server.read_notes('reader', limit=3, project=self.project)
        self.assertIn('#7 ', third)

    def test_peek_keeps_cursor(self):
        self.send()
        pid = server._resolve_project(self.project)[0]
        before = server._read_cursors(pid)
        server.read_notes('reader', peek=True, project=self.project)
        self.assertEqual(before, server._read_cursors(pid))

    def test_keep_pending_beyond_history_limit(self):
        for i in range(205): self.send(str(i))
        self.assertEqual(len(self.notes()), 205)
        self.assertEqual(self.notes()[0]['seq'], 1)

    def test_idempotent_send(self):
        self.send(request_id='retry')
        self.send(request_id='retry')
        self.assertEqual(len(self.notes()), 1)
        self.send('changed', request_id='retry')
        self.assertEqual(len(self.notes()), 1)

    def test_reject_oversize_without_truncation(self):
        self.send('x' * (server.NOTE_TEXT_MAX + 1))
        self.assertEqual(self.notes(), [])

    def test_paths_normalized(self):
        self.assertTrue(server._overlap(r'src\auth\login.py', 'src/auth/login.py'))
        self.assertTrue(server._overlap('src/../src/auth/login.py', 'src/auth/login.py'))
        self.assertTrue(server._overlap(str(Path.cwd() / 'src/auth/login.py'), 'src/auth/login.py'))
        self.assertFalse(server._overlap('src/auth', 'src/authorization'))

    def test_explicit_receipt_stages(self):
        self.send()
        server.read_notes('reader', project=self.project)
        server.ack_notes('reader', '1', project=self.project, status='processing', result='working')
        server.ack_notes('reader', '1', project=self.project, status='completed', result='verified')
        outbox = server.read_notes('author', box='outbox', project=self.project)
        self.assertIn('verified', outbox)
        server.ack_notes('reader', '1', project=self.project, status='received')
        self.assertEqual(self.notes()[0]['receipts']['reader']['status'], 'completed')

    def test_implicit_ack_does_not_ack_unseen(self):
        self.send()
        server.ack_notes('reader', project=self.project)
        self.assertEqual(self.notes()[0]['acked'], [])

    def test_corrupt_file_is_not_overwritten(self):
        self.send()
        path = server._notes_path(server._resolve_project(self.project)[0])
        with path.open('a', encoding='utf-8') as stream: stream.write('broken-json\n')
        before = path.read_bytes()
        try: self.send('new')
        except (ValueError, RuntimeError): pass
        self.assertEqual(path.read_bytes(), before)

    def test_concurrent_sends_unique_sequence(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.send(str(i)), range(16)))
        self.assertEqual([n['seq'] for n in self.notes()], list(range(1, 17)))

    def test_project_isolation(self):
        self.send()
        other = server.read_notes('reader', project=self.project + '-other', peek=True)
        self.assertNotIn('#1 ', other)

    def test_board_delivery_is_bounded(self):
        for i in range(55): self.send(str(i))
        first = server.get_board(project=self.project, agent='reader')
        self.assertIn('#1 ', first)
        self.assertNotIn('#55 ', first)
        second = server.get_board(project=self.project, agent='reader')
        self.assertIn('#55 ', second)

    def test_other_recipient_cannot_ack(self):
        self.send()
        server.ack_notes('stranger', '1', project=self.project)
        self.assertEqual(self.notes()[0]['acked'], [])

    def test_invalid_status_does_not_write(self):
        self.send()
        before = self.notes()
        server.ack_notes('reader', '1', project=self.project, status='invented')
        self.assertEqual(self.notes(), before)

    def test_corrupt_cursor_is_preserved(self):
        self.send()
        pid = server._resolve_project(self.project)[0]
        path = server._cursors_path(pid)
        path.write_text('broken-cursor', encoding='utf-8')
        with self.assertRaises(ValueError):
            server.read_notes('reader', project=self.project)
        self.assertEqual(path.read_text(encoding='utf-8'), 'broken-cursor')

    def test_legacy_note_remains_compatible(self):
        self.send()
        path = server._notes_path(server._resolve_project(self.project)[0])
        note = self.notes()[0]
        note.pop('receipts', None)
        note.pop('request_id', None)
        note['acked'] = ['reader']
        path.write_text(json.dumps(note) + '\n', encoding='utf-8')
        self.assertIn('#1 ', server.read_notes('author', box='outbox', project=self.project))
        server.ack_notes('reader', '1', project=self.project, status='completed', result='legacy verified')
        self.assertEqual(self.notes()[0]['receipts']['reader']['status'], 'completed')

    def test_mcp_stdio_roundtrip(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def exercise():
            with tempfile.TemporaryDirectory(prefix='board-protocol-') as isolated:
                env = dict(os.environ, BOARD_MCP_ROOT=isolated, BOARD_MCP_PROJECT=self.project,
                           PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
                params = StdioServerParameters(command=sys.executable,
                    args=['-B', str(Path(server.__file__).absolute())], env=env)
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listing = await session.list_tools()
                        found = {t.name: t for t in listing.tools}
                        self.assertIn('request_id', found['send_note'].inputSchema['properties'])
                        self.assertIn('status', found['ack_notes'].inputSchema['properties'])
                        sent = await session.call_tool('send_note', {'agent':'writer','to':'receiver',
                            'text':'protocol task', 'request_id':'protocol-retry'})
                        self.assertFalse(sent.isError)
                        inbox = await session.call_tool('read_notes', {'agent':'receiver'})
                        self.assertIn('protocol task', str(inbox.content))
                        ack = await session.call_tool('ack_notes', {'agent':'receiver', 'ids':'1',
                            'status':'completed', 'result':'protocol verified'})
                        self.assertFalse(ack.isError)
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        again = await session.call_tool('send_note', {'agent':'writer','to':'receiver',
                            'text':'protocol task','request_id':'protocol-retry'})
                        self.assertFalse(again.isError)
                        out = await session.call_tool('read_notes', {'agent':'writer','box':'outbox'})
                        self.assertIn('protocol verified', str(out.content))
                        persisted = list((Path(isolated) / 'notes').glob('*.jsonl'))
                        self.assertEqual(len(persisted), 1)
                        self.assertEqual(len(persisted[0].read_text(encoding='utf-8').splitlines()), 1)
        asyncio.run(exercise())

    def test_stale_heartbeat_never_kills_reused_pid(self):
        server.RUN_DIR.mkdir(parents=True, exist_ok=True)
        stale = server.RUN_DIR / 'server-stale-test.json'
        stale.write_text(json.dumps({'pid': os.getpid(), 'heartbeat': '2000-01-01 00:00:00'}), encoding='utf-8')
        with patch('os.kill') as kill:
            server._startup_cleanup()
            kill.assert_not_called()
        self.assertFalse(stale.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
