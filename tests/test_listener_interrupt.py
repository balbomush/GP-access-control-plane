"""Exercise the real listener process and its non-daemon worker teardown."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request


@unittest.skipUnless(sys.platform == 'linux', 'requires native Linux SIGINT semantics')
class ListenerInterruptTests(unittest.TestCase):
    def test_web_cli_sigint_reaps_listener_workers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            source = Path(__file__).resolve().parents[1] / 'src'
            env = {**os.environ, 'PYTHONPATH':str(source), 'HOME':raw}
            with open(Path(raw)/'web.log', 'w+') as log:
                process = subprocess.Popen([
                    sys.executable, '-c',
                    'from gp_control_plane.cli import main; raise SystemExit(main())',
                    '--state-dir',str(Path(raw)/'state'), 'web','--host','127.0.0.1',
                    '--port',str(port),'--core-url','http://127.0.0.1:1',
                ], env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic()+10
                    while True:
                        self.assertIsNone(process.poll(), 'listener exited before readiness')
                        try:
                            with urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=.5) as reply:
                                self.assertEqual(reply.status,200)
                                self.assertTrue(reply.read())
                            break
                        except (OSError, urllib.error.URLError):
                            if time.monotonic() >= deadline:
                                self.fail('listener did not become ready')
                            time.sleep(.02)
                    process.send_signal(signal.SIGINT)
                    # A traceback is expected; a lingering non-daemon worker
                    # would keep this very process alive beyond the bound.
                    self.assertIn(process.wait(timeout=10),(-signal.SIGINT,130))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
