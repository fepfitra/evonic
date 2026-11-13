"""PIT-373: a broken execution environment must fail in seconds and name the fault.

Both halves of the report are covered with fakes, because both branches were
previously unreachable in tests:

* a missing sandbox image surfaced only as `docker run` stderr (or, with the
  daemon slow to answer, as nothing at all for ~150 s), and
* an unreachable SSH workplace sat on the kernel's SYN-retry budget because the
  connect timeout was 300 s.
"""

import socket
import sys
import types

import pytest

from backend.tools.lib.backends import docker_backend
from backend.tools.lib.backends import ssh_backend


def _proc(returncode=0, stdout='', stderr=''):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _clear_image_cache():
    docker_backend._reset_image_cache()
    yield
    docker_backend._reset_image_cache()


def test_missing_image_is_named_with_the_build_command(monkeypatch):
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        return _proc(1, '', 'Error response from daemon: No such image: evonic-sandbox:latest')

    monkeypatch.setattr(docker_backend, '_docker', fake_docker)

    error = docker_backend.sandbox_image_error()

    assert 'evonic-sandbox:latest' in error
    assert 'not present on this host' in error
    assert 'docker build -t evonic-sandbox:latest' in error
    assert calls == [('image', 'inspect', 'evonic-sandbox:latest')]


def test_container_start_refuses_before_docker_run_when_image_is_missing(monkeypatch):
    """The whole point: no `docker run`, so no pull attempt and no 150 s hole."""
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        return _proc(1, '', 'Error response from daemon: No such image: evonic-sandbox:latest')

    monkeypatch.setattr(docker_backend, '_docker', fake_docker)

    container_id, error = docker_backend._get_or_create_container('pit-373-missing')

    assert container_id is None
    assert 'evonic-sandbox:latest' in error
    assert all(call[0] != 'run' for call in calls), calls


def test_present_image_is_cached_and_run_proceeds(monkeypatch):
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'image':
            return _proc(0, '[]', '')
        return _proc(0, 'container123\n', '')

    monkeypatch.setattr(docker_backend, '_docker', fake_docker)

    assert docker_backend.sandbox_image_error() == ''
    assert docker_backend.sandbox_image_error() == ''
    assert [c[0] for c in calls] == ['image'], 'a present image must be inspected once'

    container_id, error = docker_backend._get_or_create_container('pit-373-present')

    assert error is None
    assert container_id == 'container123'
    verbs = [c[0] for c in calls]
    assert 'run' in verbs, verbs
    assert verbs.count('image') == 1, 'the image is inspected once per container'


def test_docker_daemon_timeout_is_reported_as_a_named_fault(monkeypatch):
    def fake_docker(*args, **kwargs):
        raise docker_backend.subprocess.TimeoutExpired(cmd='docker', timeout=5)

    monkeypatch.setattr(docker_backend, '_docker', fake_docker)

    error = docker_backend.sandbox_image_error()

    assert 'did not answer within' in error
    assert 'Docker daemon is not responding' in error


def test_ssh_connect_is_bounded_and_names_host_and_port(monkeypatch):
    captured = {}

    class FakeSSHClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            captured.update(kwargs)
            raise socket.timeout('timed out')

    monkeypatch.setitem(sys.modules, 'paramiko', types.SimpleNamespace(
        SSHClient=FakeSSHClient, AutoAddPolicy=lambda: None))

    with pytest.raises(RuntimeError) as excinfo:
        ssh_backend.SSHBackend(host='100.99.99.99', username='fep', port=2222,
                               key_path='~/.ssh/id_test', session_id='pit-373')

    assert captured['timeout'] == ssh_backend.SSH_CONNECT_TIMEOUT
    assert captured['banner_timeout'] == ssh_backend.SSH_CONNECT_TIMEOUT
    message = str(excinfo.value)
    assert 'fep@100.99.99.99:2222' in message
    assert f'within {ssh_backend.SSH_CONNECT_TIMEOUT}s' in message
    assert 'timed out' in message
