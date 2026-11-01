"""Pinned archive and endpoint checks only; no Docker, Linux or network calls."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import tarfile
from types import SimpleNamespace

import pytest


@pytest.fixture
def tools(monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "deployment/linux"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("tested_readiness_docker", folder / "readiness_docker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_bytes(tools, *, extra=None, omit=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        folder = tarfile.TarInfo("docker")
        folder.type = tarfile.DIRTYPE
        archive.addfile(folder)
        for name in sorted(tools.MEMBERS):
            if name == omit:
                continue
            raw = ("synthetic executable fixture " + name).encode()
            entry = tarfile.TarInfo("docker/" + name)
            entry.size = len(raw)
            archive.addfile(entry, io.BytesIO(raw))
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b"x" * min(extra.size, 1024)))
    return output.getvalue()


@pytest.fixture
def pinned_fixture(tools, monkeypatch, tmp_path):
    raw = archive_bytes(tools)
    hashes = {name: hashlib.sha256(("synthetic executable fixture " + name).encode()).hexdigest()
              for name in tools.MEMBERS}
    monkeypatch.setattr(tools, "ARCHIVE_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(tools, "ARCHIVE_BYTES", len(raw))
    monkeypatch.setattr(tools, "BUNDLE_BINARY_SHA256", hashes)
    path = tmp_path / "bundle.tgz"
    path.write_bytes(raw)
    yield path, hashes
    for entry in tmp_path.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            entry.chmod(0o600)
        elif entry.is_dir():
            entry.chmod(0o700)


def test_official_archive_and_binary_pins_are_shared_with_engine(tools):
    import readiness_engine
    assert tools.URL == "https://download.docker.com/linux/static/stable/x86_64/docker-29.9.2.tgz"
    assert tools.ARCHIVE_SHA256 == readiness_engine.BUNDLE_SHA256
    assert tools.ARCHIVE_BYTES == readiness_engine.BUNDLE_BYTES == 86374273
    assert tools.MEMBERS == readiness_engine.BUNDLE_FILES
    assert set(tools.BUNDLE_BINARY_SHA256) == tools.MEMBERS


@pytest.mark.parametrize("address", ["127.0.0.1", "10.20.30.45", "192.168.1.1", "169.254.169.254", "0.0.0.0"])  # noqa: S104 - rejected DNS data, never a bind.
def test_private_resolution_is_rejected_before_any_connection(tools, monkeypatch, address):
    monkeypatch.setattr(tools.socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443))])
    monkeypatch.setattr(tools.socket, "create_connection", lambda *a, **k: pytest.fail("No TCP to private endpoint"))
    with pytest.raises(tools.DockerToolsError, match="PUBLIC_ENDPOINT_REQUIRED"):
        tools.public_address()


def test_mixed_dns_answers_do_not_allow_a_private_fallback(tools, monkeypatch):
    rows = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443)) for address in ("8.8.8.8", "10.20.30.45")]
    monkeypatch.setattr(tools.socket, "getaddrinfo", lambda *a, **k: rows)
    with pytest.raises(tools.DockerToolsError, match="PUBLIC_ENDPOINT_REQUIRED"):
        tools.public_address()


def test_resolved_public_address_is_selected_once_without_connecting(tools, monkeypatch):
    calls = []
    def resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]
    monkeypatch.setattr(tools.socket, "getaddrinfo", resolve)
    assert tools.public_address() == "8.8.8.8" and len(calls) == 1
    assert calls[0][1] == {"family": socket.AF_INET, "type": socket.SOCK_STREAM}


def test_guard_requires_an_explicit_new_ephemeral_root(tools, monkeypatch, tmp_path):
    monkeypatch.setattr(tools.sys, "platform", "linux")
    monkeypatch.setattr(tools.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    tools.guard(tmp_path / "new-owned-tools")
    assert not (tmp_path / "new-owned-tools").exists()
    for path in (tmp_path, tmp_path.parent / "outside", tmp_path / "a/../escape"):
        with pytest.raises(tools.DockerToolsError, match="EPHEMERAL_ROOT_REQUIRED"):
            tools.guard(path)
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    with pytest.raises(tools.DockerToolsError, match="EPHEMERAL_ROOT_REQUIRED"):
        tools.guard(tmp_path / "new-owned-tools")


@pytest.mark.parametrize("name,kind", [
    ("docker/dockerd", tarfile.REGTYPE), ("docker/link", tarfile.SYMTYPE),
    ("docker/link", tarfile.LNKTYPE), ("../escape", tarfile.REGTYPE),
    ("/docker/escape", tarfile.REGTYPE), ("docker/extra", tarfile.REGTYPE),
])
def test_all_headers_are_checked_before_extracting_unknown_or_linked_members(tools, name, kind):
    entry = tarfile.TarInfo(name)
    entry.type = kind
    entry.linkname = "/unrelated"
    entry.size = 1 if kind == tarfile.REGTYPE else 0
    raw = archive_bytes(tools, extra=entry)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        with pytest.raises(tools.DockerToolsError, match="ARCHIVE_MEMBER_INVALID"):
            tools.validated_members(archive)


def test_missing_binary_is_rejected_even_when_archive_has_only_regular_files(tools):
    with tarfile.open(fileobj=io.BytesIO(archive_bytes(tools, omit="dockerd")), mode="r:gz") as archive:
        with pytest.raises(tools.DockerToolsError, match="ARCHIVE_MEMBERS_INVALID"):
            tools.validated_members(archive)


def test_pinned_extraction_copies_exact_bytes_without_executing_or_overwriting(tools, monkeypatch, pinned_fixture, tmp_path):
    source, hashes = pinned_fixture
    monkeypatch.setattr(tools.tarfile.TarFile, "extractall", lambda *a, **k: pytest.fail("No generic tar extraction"))
    destination = tmp_path / "bin"
    assert tools.extract(source, destination) == hashes
    assert {path.name for path in destination.iterdir()} == tools.MEMBERS
    for name in tools.MEMBERS:
        assert (destination / name).read_bytes() == ("synthetic executable fixture " + name).encode()
    with pytest.raises(tools.DockerToolsError, match="ARCHIVE_PIN_MISMATCH"):
        tools.extract(source, destination)


def test_archive_pin_mismatch_is_rejected_before_destination_creation(tools, pinned_fixture, tmp_path):
    source, _ = pinned_fixture
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(tools.DockerToolsError, match="ARCHIVE_PIN_MISMATCH"):
        tools.extract(source, tmp_path / "bin")
    assert not (tmp_path / "bin").exists()


def test_extracted_binary_must_match_fixed_manifest_not_a_forged_marker(tools, monkeypatch, pinned_fixture, tmp_path):
    source, _ = pinned_fixture
    monkeypatch.setattr(tools, "BUNDLE_BINARY_SHA256", dict.fromkeys(tools.MEMBERS, "0" * 64))
    with pytest.raises(tools.DockerToolsError, match="BINARY_PIN_MISMATCH"):
        tools.extract(source, tmp_path / "bin")


def test_prepare_seals_exact_shared_marker_and_never_installs_system_paths(tools, monkeypatch, pinned_fixture, tmp_path):
    source, hashes = pinned_fixture
    monkeypatch.setattr(tools, "guard", lambda root: None)
    monkeypatch.setattr(tools, "download", lambda path: path.write_bytes(source.read_bytes()))
    root = tmp_path / "owned-tools"
    binaries, proof = tools.prepare(root)
    marker = json.loads((binaries / "bundle.json").read_text())
    assert binaries == root / "bin" and marker == {
        "contract": tools.BUNDLE_CONTRACT, "version": tools.VERSION,
        "archive_sha256": tools.ARCHIVE_SHA256, "archive_bytes": tools.ARCHIVE_BYTES, "binaries": hashes}
    assert {path.name for path in binaries.iterdir()} == tools.MEMBERS | {"bundle.json"}
    assert proof["system_binaries_changed"] is False and proof["host_daemon_used"] is False


@pytest.mark.parametrize("status", [200, 302])
def test_download_uses_one_resolved_public_socket_with_official_tls_name_and_no_redirect(tools, monkeypatch, tmp_path, status):
    raw, calls = b"synthetic pinned archive fixture", []
    stream = io.BytesIO(raw)
    response = SimpleNamespace(status=status, getheader=lambda name: str(len(raw)), read=stream.read)
    transport = SimpleNamespace(close=lambda: calls.append("socket_closed"))

    def connect(endpoint, **options):
        assert endpoint == ("8.8.8.8", 443) and options == {"timeout": 30}
        calls.append("connect")
        return transport

    def secure(sock, *, server_hostname):
        assert sock is transport and server_hostname == "download.docker.com"
        calls.append("tls_official_hostname")
        return transport

    class Connection:
        def __init__(self, host, *, timeout, context):
            assert host == tools.HOST and timeout == 30

        def request(self, method, path, *, headers):
            assert method == "GET" and path == tools.RESOURCE and self.auto_open is False
            assert headers["Accept-Encoding"] == "identity" and "Authorization" not in headers
            calls.append("GET")

        def getresponse(self):
            return response

        def close(self):
            calls.append("connection_closed")

    monkeypatch.setattr(tools, "public_address", lambda: "8.8.8.8")
    monkeypatch.setattr(tools.socket, "create_connection", connect)
    monkeypatch.setattr(tools.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=secure))
    monkeypatch.setattr(tools.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(tools, "ARCHIVE_BYTES", len(raw))
    monkeypatch.setattr(tools, "ARCHIVE_SHA256", hashlib.sha256(raw).hexdigest())
    path = tmp_path / "bundle.tgz"
    if status == 200:
        tools.download(path)
        assert path.read_bytes() == raw
    else:
        with pytest.raises(tools.DockerToolsError, match="DOWNLOAD_RESPONSE_INVALID"):
            tools.download(path)
        assert not path.exists()
    assert calls.count("connect") == calls.count("GET") == 1
    assert "tls_official_hostname" in calls and "socket_closed" in calls and "connection_closed" in calls
