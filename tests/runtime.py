#!/usr/bin/env python3
"""Exercise official packages in an isolated Linux container, as a non-root UID.

Mount packages at /packages:ro and this directory at /tests:ro, then run:
  python3 /tests/runtime.py /packages/amd64
Only temporary files under /tmp and local loopback requests are used.
"""

import contextlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

CLIENT = "pier-check-client-7bf162"
NEXT_CLIENT = "pier-check-client-next-df4013"
MANAGEMENT = "pier-check-management-25d694"


def request(port, path, key=None, method="GET", body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key
    if body is not None:
        body = json.dumps(body)
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class Service:
    def __init__(self, package, data, port, client):
        self.package = package
        self.data = data
        self.port = port
        self.client = client
        self.process = None

    def spawn(self):
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [str(self.package / "bin/start.sh")],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PIER_DATA_DIR": str(self.data)},
            stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True,
        )

    def failure(self, message):
        self.log.seek(0)
        raise AssertionError(message + "\n" + self.log.read().decode(errors="replace"))

    def start(self):
        self.spawn()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.failure("service exited before readiness")
            try:
                if request(self.port, "/v1/models", self.client)[0] == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        self.failure("service did not become ready")

    def stop(self):
        try:
            if self.process is not None and self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
                    self.failure("service ignored SIGTERM")
        finally:
            if hasattr(self, "log"):
                self.log.close()

    def fails_during_startup(self):
        self.spawn()
        try:
            self.process.wait(timeout=9)
        except subprocess.TimeoutExpired:
            self.failure("invalid startup survived Pier's default 10-second observation window")
        finally:
            self.stop()

    def strategy(self):
        status, body = request(self.port, "/v0/management/routing/strategy", MANAGEMENT)
        assert status == 200, (status, body)
        return json.loads(body)["strategy"]


def main():
    assert os.geteuid() != 0, "run runtime tests as an unprivileged container UID"
    packages = Path(sys.argv[1]).resolve()
    first = packages / "unpacked"
    second = packages / "next/unpacked"
    for package in [first, second]:
        assert not os.access(package / "configs/config.yaml", os.W_OK), "mount packages read-only"

    with tempfile.TemporaryDirectory(prefix="cliproxyapi-runtime-") as temporary:
        data = Path(temporary) / "data"
        (data / "static").mkdir(parents=True)
        panel = data / "static/management.html"
        panel.write_text("<!doctype html><title>Cached management fixture</title>")
        config = data / "runtime/config.yaml"
        marker = data / "runtime/pier-release"
        one = Service(first, data, 18317, CLIENT)
        two = Service(second, data, 18318, NEXT_CLIENT)
        try:
            one.start()
            assert config.stat().st_mode & 0o777 == 0o600
            assert MANAGEMENT not in config.read_text(), "upstream should hash the management key"
            assert marker.read_text().strip() == str(first)
            assert request(18317, "/v1/models")[0] == 401
            assert request(18317, "/v1/models", MANAGEMENT)[0] == 401
            assert request(18317, "/v8/management/config")[0] == 401
            assert request(18317, "/v8/management/config", MANAGEMENT)[0] == 200
            assert request(18317, "/management.html")[1] == panel.read_bytes()
            listeners = [line.split()[1] for line in Path("/proc/net/tcp").read_text().splitlines()[1:]
                         if line.split()[3] == "0A" and int(line.split()[1].split(":")[1], 16) == 18317]
            assert listeners == ["0100007F:%04X" % 18317], listeners
            # Verify the process survives Pier's normal startup observation period.
            time.sleep(10)
            assert one.process.poll() is None
            credential = data / "auths/credential.fixture"
            credential.write_bytes(b"persistent credential fixture")
            assert one.strategy() == "round-robin"
            status, body = request(18317, "/v0/management/routing/strategy", MANAGEMENT,
                                   "PUT", {"value": "fill-first"})
            assert status == 200, (status, body)
            one.stop()
            assert "fill-first" in config.read_text()
            one.start()
            assert one.strategy() == "fill-first", "ordinary restart lost management changes"
            one.stop()

            two.start()
            assert marker.read_text().strip() == str(second)
            assert two.strategy() == "round-robin", "redeploy failed to restore package config"
            assert request(18318, "/v1/models", CLIENT)[0] == 401
            two.stop()
            one.start()
            assert one.strategy() == "round-robin"
            assert marker.read_text().strip() == str(first)
            assert request(18317, "/v1/models", NEXT_CLIENT)[0] == 401
            one.stop()
            assert credential.read_bytes() == b"persistent credential fixture"
            assert "Cached management fixture" in panel.read_text()

            config.write_text("server: [invalid YAML\n")
            one.fails_during_startup()
            # Roll back to a different release path to recover package configuration.
            two.start()
            two.stop()
            with contextlib.closing(socket.socket()) as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(("127.0.0.1", 18317))
                occupied.listen()
                one.fails_during_startup()
            one.start()
            one.stop()
            assert credential.read_bytes() == b"persistent credential fixture"
            print("PASS runtime: unprivileged/read-only, API and management auth, loopback, config writes, restart, redeploy, rollback, persistence, invalid config, occupied port, SIGTERM")
        finally:
            one.stop()
            two.stop()


if __name__ == "__main__":
    main()
