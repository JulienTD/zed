import copy
import io
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import unittest
from unittest.mock import patch, mock_open
import codex_writer as writer


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.owner = dict(pid=123, start_time='1000', lock_identity=[8, 1, 42],
                          sessions=['session'], executable='/bin/codex')
        self.inspection = dict(owner=self.owner, reason=None)

    def test_only_kernel_write_locks_are_owners(self):
        locks = ('1: FLOCK ADVISORY WRITE 123 08:01:42 0 EOF\n'
                 '2: -> FLOCK ADVISORY WRITE 999 08:01:42 0 EOF\n'
                 '3: FLOCK ADVISORY READ 456 08:01:43 0 EOF\n')
        with patch.object(writer.Path, 'read_text', return_value=locks):
            self.assertEqual(writer.kernel_locks(), {(8, 1, 42): 123})

    def test_invalid_session_id_never_reads_locks(self):
        with patch.object(writer, 'kernel_locks') as locks:
            with self.assertRaises(ValueError):
                writer.inspect('../../another-session')
            locks.assert_not_called()

    def test_different_owner_is_never_signalled(self):
        previous = dict(self.owner, pid=999)
        with patch.object(writer, 'inspect', return_value=self.inspection), \
             patch.object(writer.signal, 'pidfd_send_signal', create=True) as send:
            with self.assertRaisesRegex(RuntimeError, 'owner changed'):
                writer.stop('session', previous)
            send.assert_not_called()

    def test_shared_process_is_never_signalled(self):
        with patch.object(writer, 'inspect', return_value=dict(self.inspection, reason='owns two conversations')), \
             patch.object(writer.signal, 'pidfd_send_signal', create=True) as send:
            with self.assertRaises(RuntimeError):
                writer.stop('session', self.owner)
            send.assert_not_called()

    def test_owner_rechecked_after_opening_pidfd(self):
        changed = dict(self.inspection, owner=dict(self.owner, start_time='2000'))
        with patch.object(writer, 'inspect', side_effect=[self.inspection, changed]), \
             patch.object(writer.os, 'pidfd_open', return_value=77, create=True), \
             patch.object(writer.os, 'fdopen', return_value=io.BytesIO()), \
             patch.object(writer.signal, 'pidfd_send_signal', create=True) as send:
            with self.assertRaisesRegex(RuntimeError, 'owner changed'):
                writer.stop('session', self.owner)
            send.assert_not_called()

    def test_confirmed_owner_gets_sigterm_once_and_lock_is_not_deleted(self):
        handle = mock_open().return_value
        handle.fileno.return_value = 77
        with patch.object(writer, 'inspect', return_value=self.inspection), \
             patch.object(writer.os, 'pidfd_open', return_value=77, create=True), \
             patch.object(writer.os, 'fdopen', return_value=handle), \
             patch.object(writer.signal, 'pidfd_send_signal', create=True) as send, \
             patch.object(writer, 'kernel_locks', return_value={}), \
             patch.object(writer.Path, 'unlink') as unlink:
            writer.stop('session', copy.deepcopy(self.owner))
            send.assert_called_once_with(77, writer.signal.SIGTERM)
            unlink.assert_not_called()

    def test_stuck_owner_reports_timeout_without_force_kill(self):
        handle = mock_open().return_value
        handle.fileno.return_value = 77
        with patch.object(writer, 'inspect', return_value=self.inspection), \
             patch.object(writer.os, 'pidfd_open', return_value=77, create=True), \
             patch.object(writer.os, 'fdopen', return_value=handle), \
             patch.object(writer.signal, 'pidfd_send_signal', create=True) as send, \
             patch.object(writer.time, 'monotonic', side_effect=[0, 9]):
            with self.assertRaisesRegex(RuntimeError, 'still holds the session'):
                writer.stop('session', self.owner)
            send.assert_called_once_with(77, writer.signal.SIGTERM)


@unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'pidfd_open'), 'requires Linux pidfds')
class LinuxWriterTests(unittest.TestCase):
    @contextlib.contextmanager
    def writer_process(self, count=1):
        with tempfile.TemporaryDirectory(prefix='zed-writer-test-') as directory:
            directory = Path(directory)
            executable = directory / 'codex'
            shutil.copy2(sys.executable, executable)
            locks = directory / 'thread-writer-locks'
            locks.mkdir()
            sessions = [str(uuid.uuid4()) for _ in range(count)]
            body = ("import fcntl,sys,time; files=[open(p,'w') for p in sys.argv[1:]]; "
                    "[fcntl.flock(f,fcntl.LOCK_EX) for f in files]; "
                    "print('ready',flush=True); time.sleep(30)")
            child = subprocess.Popen([str(executable), '-c', body,
                                      *[str(locks / (session + '.lock')) for session in sessions]],
                                     stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with patch.dict(os.environ, CODEX_HOME=str(directory)):
                    yield child, sessions, locks
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=3)
                child.stdout.close()

    def test_real_writer_takeover(self):
        with self.writer_process() as (child, sessions, locks):
            report = writer.inspect(sessions[0])
            self.assertIsNone(report['reason'])
            self.assertEqual(report['owner']['pid'], child.pid)
            with self.assertRaises(RuntimeError):
                writer.stop(sessions[0], dict(report['owner'], start_time='wrong'))
            self.assertIsNone(child.poll())
            writer.stop(sessions[0], report['owner'])
            self.assertEqual(child.wait(timeout=3), -writer.signal.SIGTERM)
            self.assertTrue((locks / (sessions[0] + '.lock')).exists())

    def test_real_process_with_two_sessions_is_not_stopped(self):
        with self.writer_process(count=2) as (child, sessions, _):
            report = writer.inspect(sessions[0])
            self.assertIn('2 conversations', report['reason'])
            with self.assertRaises(RuntimeError):
                writer.stop(sessions[0], report['owner'])
            self.assertIsNone(child.poll())


if __name__ == '__main__':
    unittest.main()
