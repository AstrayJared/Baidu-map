import gc
import ipaddress
import socket
import threading
from asyncio import proactor_events, selector_events

import pytest

# External connections are refused for the whole test session, before any app module
# is imported: backend/.env may hold a real AK, and a test that forgot to inject a fake
# provider must fail loudly instead of spending quota. Loopback stays open for the
# in-process servers some tests start.
EXTERNAL = {"attempts": 0, "targets": []}
_GUARD_LOCK = threading.Lock()


def _refuse(target) -> None:
    with _GUARD_LOCK:
        EXTERNAL["attempts"] += 1
        EXTERNAL["targets"].append(str(target)[:80])
    raise ConnectionRefusedError("test session: external connections are refused")


def _loopback(host) -> bool:
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _address_host(address):
    return address[0] if isinstance(address, tuple) else address


_getaddrinfo = socket.getaddrinfo


def _guarded_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not _loopback(host):
        _refuse(host)
    return _getaddrinfo(host, *args, **kwargs)


def _guard_connect(original):
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(_address_host(address)):
            _refuse(_address_host(address))
        return original(self, address)
    return connect


def _guard_sock_connect(original):
    async def sock_connect(self, sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(_address_host(address)):
            _refuse(_address_host(address))
        return await original(self, sock, address)
    return sock_connect


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guard_connect(socket.socket.connect)
socket.socket.connect_ex = _guard_connect(socket.socket.connect_ex)
# On Windows asyncio connects through ConnectEx, not socket.connect: guard both loops.
for _loop in (proactor_events.BaseProactorEventLoop, selector_events.BaseSelectorEventLoop):
    _loop.sock_connect = _guard_sock_connect(_loop.sock_connect)


def pytest_terminal_summary(terminalreporter):
    if EXTERNAL["attempts"]:
        terminalreporter.write_line(
            f"external connection attempts refused: {EXTERNAL['attempts']} "
            f"(first targets: {EXTERNAL['targets'][:5]})", red=True)


@pytest.fixture(autouse=True)
def _thaw_the_heap():
    """Loading the offline graph freezes the heap: in service it lives as long as the
    process. Tests load many small graphs; thawed after each, whatever a test leaves
    behind is still collected."""
    yield
    gc.unfreeze()


@pytest.fixture(scope="session", autouse=True)
def _scratch_deployment_paths(tmp_path_factory):
    """Keep an explicitly-configured ``Settings`` off the deployment's own files.

    ``Settings`` names the paths a real deployment uses, so a test that writes
    ``Settings(_env_file=None, ...)`` without overriding them aims its ledger at
    ``backend/.quota/quota.sqlite3`` and its checkup store at ``backend/.checkups``.
    Constructing them is enough to create the schema, and a test that reserved an
    attempt would spend the deployment's own day — the accounting this suite exists
    to protect. A test is about behaviour, not about which directory the deployment
    happens to use.

    Two limits keep the redirect honest:

    * Only a construction that *asks* for an explicit configuration is redirected,
      and a path the test itself passes always wins, so a test that means to point
      somewhere specific still does.
    * ``load_settings()`` is untouched. A test that calls it is asking about the
      ambient deployment configuration, and must keep getting the real answer.
    """
    from app.config import Settings

    root = tmp_path_factory.mktemp("deployment-paths")
    scratch = {"quota_ledger_path": str(root / "quota.sqlite3"),
               "checkup_dir": str(root / "checkups"),
               "hybrid_ledger_dir": str(root / "ledgers")}
    original = Settings.__init__

    def isolated(self, **values):
        if "_env_file" in values:
            for name, path in scratch.items():
                values.setdefault(name, path)
        original(self, **values)

    patcher = pytest.MonkeyPatch()
    patcher.setattr(Settings, "__init__", isolated)
    try:
        yield root
    finally:
        patcher.undo()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Keep portable slow-test evidence without native stack unwinding.

    Windows crashed while the native faulthandler timer inspected a GEOS worker.
    Python frames are sufficient to locate the stage and retain the test node;
    reading them under the GIL avoids that diagnostic path. This is not a timeout
    extension or a substitute for assertions on completion.
    """
    import sys
    import traceback

    def dump():
        import hashlib
        from pathlib import Path
        import os

        directory = Path(__file__).resolve().parents[1]/'logs'/'slow-tests'
        directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(item.nodeid.encode()).hexdigest()[:16]
        path = directory/f'{os.getpid()}-{name}.log'
        with path.open('w', encoding='utf-8') as stream:
            print(f"Slow test after 120 seconds: {item.nodeid}", file=stream)
            for thread_id, frame in sys._current_frames().items():
                print(f"Thread {thread_id}:", file=stream)
                traceback.print_stack(frame, file=stream)
        print(f"\nSlow-test Python stacks: {path}", file=sys.stderr, flush=True)

    timer = threading.Timer(120, dump)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
