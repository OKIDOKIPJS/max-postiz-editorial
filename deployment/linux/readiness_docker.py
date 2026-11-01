"""Pinned Docker tools for a new GitHub-hosted engine, never a system install."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import sys
import tarfile
import time

from readiness_engine import (
    BUNDLE_BINARY_SHA256, BUNDLE_BYTES, BUNDLE_CONTRACT, BUNDLE_FILES, BUNDLE_SHA256, BUNDLE_VERSION,
)

VERSION = BUNDLE_VERSION
HOST = "download.docker.com"
RESOURCE = "/linux/static/stable/x86_64/docker-29.9.2.tgz"
URL = "https://" + HOST + RESOURCE
ARCHIVE_SHA256 = BUNDLE_SHA256
ARCHIVE_BYTES = BUNDLE_BYTES
MEMBERS = BUNDLE_FILES
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 300 * 1024 * 1024


class DockerToolsError(RuntimeError):
    """Only stable codes escape the private downloader."""


def guard(root):
    base = Path(os.environ.get("RUNNER_TEMP", ""))
    root = Path(root)
    if (sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or not base.is_absolute() or not base.is_dir() or not root.is_absolute()
            or root.exists() or ".." in root.parts or root.resolve() == base.resolve()
            or not root.resolve().is_relative_to(base.resolve())
            or any(path.is_symlink() for path in (root, *root.parents))):
        raise DockerToolsError("DOCKER_TOOLS_EPHEMERAL_ROOT_REQUIRED")


def public_address():
    """Resolve once; the subsequent TLS connection uses this exact public IP."""
    try:
        rows = socket.getaddrinfo(HOST, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
        values = [row[4][0] for row in rows]
        if not values or any(not ipaddress.ip_address(value).is_global for value in values):
            raise ValueError
        return values[0]
    except (OSError, ValueError):
        raise DockerToolsError("DOCKER_TOOLS_PUBLIC_ENDPOINT_REQUIRED") from None


def download(path):
    connection = None
    transport = None
    try:
        address = public_address()
        context = ssl.create_default_context()
        transport = socket.create_connection((address, 443), timeout=30)
        connection = http.client.HTTPSConnection(HOST, timeout=30, context=context)
        connection.sock = context.wrap_socket(transport, server_hostname=HOST)
        connection.auto_open = False
        connection.request("GET", RESOURCE, headers={"Accept-Encoding": "identity", "User-Agent": "editorial-readiness/0.14.0"})
        response = connection.getresponse()
        # No redirects, inherited proxy, credentials, or second request.
        if response.status != 200 or response.getheader("Content-Length") != str(ARCHIVE_BYTES):
            raise DockerToolsError("DOCKER_TOOLS_DOWNLOAD_RESPONSE_INVALID")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        sha, total, deadline = hashlib.sha256(), 0, time.monotonic() + 180
        with os.fdopen(descriptor, "wb") as output:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerToolsError("DOCKER_TOOLS_DOWNLOAD_TIMEOUT")
                raw = response.read(1024 * 1024)
                if not raw:
                    break
                total += len(raw)
                if total > ARCHIVE_BYTES:
                    raise DockerToolsError("DOCKER_TOOLS_ARCHIVE_SIZE_INVALID")
                sha.update(raw)
                output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if total != ARCHIVE_BYTES or sha.hexdigest() != ARCHIVE_SHA256:
            raise DockerToolsError("DOCKER_TOOLS_ARCHIVE_PIN_MISMATCH")
    except DockerToolsError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise DockerToolsError("DOCKER_TOOLS_DOWNLOAD_FAILED") from None
    finally:
        if connection is not None:
            connection.close()
        if transport is not None:
            transport.close()


def validated_members(archive):
    """Validate every header before creating a destination or extracting bytes."""
    rows, names, total, directories = [], set(), 0, 0
    for member in archive.getmembers():
        if member.isdir() and member.name in {"docker", "docker/"}:
            directories += 1
            continue
        name = member.name
        if (not member.isfile() or member.issparse() or not name.startswith("docker/")
                or name.count("/") != 1 or name[7:] not in MEMBERS or name in names
                or not 0 < member.size <= MAX_MEMBER_BYTES):
            raise DockerToolsError("DOCKER_TOOLS_ARCHIVE_MEMBER_INVALID")
        names.add(name)
        total += member.size
        rows.append(member)
    if directories != 1 or {name[7:] for name in names} != MEMBERS or total > MAX_TOTAL_BYTES:
        raise DockerToolsError("DOCKER_TOOLS_ARCHIVE_MEMBERS_INVALID")
    return rows


def extract(archive_path, destination):
    archive_path, destination = Path(archive_path), Path(destination)
    info = archive_path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size != ARCHIVE_BYTES
            or destination.exists() or destination.is_symlink()):
        raise DockerToolsError("DOCKER_TOOLS_ARCHIVE_PIN_MISMATCH")
    with archive_path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != ARCHIVE_SHA256:
            raise DockerToolsError("DOCKER_TOOLS_ARCHIVE_PIN_MISMATCH")
    hashes = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        rows = validated_members(archive)
        destination.mkdir(mode=0o700)
        for member in rows:
            path = destination / member.name[7:]
            source = archive.extractfile(member)
            if source is None:
                raise DockerToolsError("DOCKER_TOOLS_ARCHIVE_MEMBER_INVALID")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o500)
            sha, total = hashlib.sha256(), 0
            with source, os.fdopen(descriptor, "wb") as output:
                while raw := source.read(1024 * 1024):
                    total += len(raw)
                    if total > member.size:
                        raise DockerToolsError("DOCKER_TOOLS_ARCHIVE_MEMBER_INVALID")
                    output.write(raw)
                    sha.update(raw)
                if total != member.size:
                    raise DockerToolsError("DOCKER_TOOLS_ARCHIVE_MEMBER_INVALID")
                output.flush()
                os.fsync(output.fileno())
            path.chmod(0o555)
            hashes[path.name] = sha.hexdigest()
            if hashes[path.name] != BUNDLE_BINARY_SHA256[path.name]:
                raise DockerToolsError("DOCKER_TOOLS_BINARY_PIN_MISMATCH")
    return hashes


def prepare(root):
    root = Path(root)
    guard(root)
    root.mkdir(mode=0o700)
    archive = root / ("docker-" + VERSION + ".tgz")
    download(archive)
    binaries = root / "bin"
    hashes = extract(archive, binaries)
    marker = {"contract": BUNDLE_CONTRACT, "version": VERSION,
              "archive_sha256": ARCHIVE_SHA256, "archive_bytes": ARCHIVE_BYTES, "binaries": hashes}
    descriptor = os.open(binaries / "bundle.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(marker, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    binaries.chmod(0o555)
    proof = {"version": VERSION, "url": URL, "sha256": ARCHIVE_SHA256,
             "bytes": ARCHIVE_BYTES, "member_count": len(hashes), "binary_sha256": hashes,
             "system_binaries_changed": False, "host_daemon_used": False}
    descriptor = os.open(root / "bundle-proof.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(proof, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    return binaries, proof
