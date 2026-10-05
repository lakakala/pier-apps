#!/usr/bin/env python3
"""Check Pier recipes and launcher behavior; optionally package official releases."""

import argparse
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def build_helper(pier, work):
    project = work / "helper"
    (project / "src").mkdir(parents=True)
    package_path = pier / "crates/pier-pkg"
    (project / "Cargo.toml").write_text(
        '[package]\nname = "pier-apps-check"\nversion = "0.1.0"\nedition = "2024"\n'
        '[dependencies]\npier-pkg = { path = ' + json.dumps(str(package_path)) + ' }\n'
        'minijinja = { version = "2.24", features = ["json"] }\n'
        'serde_json = "1"\nserde_yaml_ng = "0.10"\ntempfile = "3"\n'
    )
    shutil.copyfile(pier / "Cargo.lock", project / "Cargo.lock")
    shutil.copyfile(ROOT / "tests/pier_check.rs", project / "src/main.rs")
    target = ROOT / ".cache/check-target"
    run("cargo", "build", "--offline", "--quiet", "--manifest-path", str(project / "Cargo.toml"), "--target-dir", str(target))
    return target / "debug/pier-apps-check"


def launcher_checks(work):
    data = work / "data with spaces"
    data.mkdir()
    releases = []
    for name in ["first release", "second release"]:
        release = work / name
        (release / "bin").mkdir(parents=True)
        (release / "configs").mkdir()
        shutil.copyfile(ROOT / "apps/cliproxyapi/scripts/start.sh", release / "bin/start.sh")
        (release / "bin/start.sh").chmod(0o755)
        (release / "bin/cli-proxy-api").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$PWD" "$WRITABLE_PATH" "$MANAGEMENT_STATIC_PATH" "$@"\n'
        )
        (release / "bin/cli-proxy-api").chmod(0o755)
        (release / "configs/config.yaml").write_text(name + "\n")
        releases.append(release)
    child_env = {"PATH": "/usr/bin:/bin", "PIER_DATA_DIR": str(data)}

    def start(release, *args):
        return run(str(release / "bin/start.sh"), *args, env=child_env, capture_output=True).stdout.splitlines()

    first, second = releases
    output = start(first, "-no-browser")
    config = data / "runtime/config.yaml"
    assert output == [str(data), str(data), str(data / "static"), "-config", str(config), "-no-browser"]
    assert config.stat().st_mode & 0o777 == 0o600
    assert (data / "runtime/pier-release").read_text().strip() == str(first)
    assert config.read_text() == "first release\n"
    (data / "auths/credential.fixture").write_text("keep credential")
    (data / "static/management.html").write_text("keep panel")
    config.write_text("panel change\n")
    start(first)
    assert config.read_text() == "panel change\n"
    start(second)
    assert config.read_text() == "second release\n"
    start(first)
    assert config.read_text() == "first release\n"
    assert (data / "auths/credential.fixture").read_text() == "keep credential"
    assert (data / "static/management.html").read_text() == "keep panel"
    config.unlink()
    start(first)
    assert config.read_text() == "first release\n"

    # Interrupted/failed initialization must not commit the release marker.
    (second / "configs/config.yaml").write_text("")
    failure = subprocess.run([str(second / "bin/start.sh")], env=child_env, capture_output=True)
    assert failure.returncode != 0
    assert config.read_text() == "first release\n"
    assert (data / "runtime/pier-release").read_text().strip() == str(first)
    assert not list((data / "runtime").glob(".init.*"))
    for invalid_env in [{"PATH": "/usr/bin:/bin"}, {"PATH": "/usr/bin:/bin", "PIER_DATA_DIR": "relative"}]:
        failure = subprocess.run([str(first / "bin/start.sh")], env=invalid_env, capture_output=True)
        assert failure.returncode != 0
    print("PASS launcher: initialization, restart, redeploy, rollback, persistence, failure recovery", flush=True)


def download(item, cache):
    target = cache / item["url"].rsplit("/", 1)[-1]
    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
        with urllib.request.urlopen(item["url"], timeout=60) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise RuntimeError("official release checksum mismatch")
        temporary = target.with_suffix(".partial")
        temporary.write_bytes(content)
        temporary.replace(target)
    with tarfile.open(target) as archive:
        assert {"cli-proxy-api", "LICENSE", "config.example.yaml"} <= set(archive.getnames())
    return target


class CacheHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pier-repo", type=Path, default=ROOT.parent / "pier", help="local Pier source checkout (default: ../pier)")
    parser.add_argument("--release-tests", action="store_true", help="download/checksum both official archives and pack/unpack with pier-pkg")
    parser.add_argument("--packages-dir", type=Path, help="keep unpacked test packages here for runtime.py; directory must not exist")
    args = parser.parse_args()
    if args.packages_dir and not args.release_tests:
        parser.error("--packages-dir requires --release-tests")
    cache = ROOT / ".cache/releases"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pier-apps-check-") as temporary:
        work = Path(temporary)
        helper = build_helper(args.pier_repo.resolve(), work)
        metadata = json.loads(run(str(helper), "check", str(ROOT), capture_output=True).stdout)
        print("PASS recipes: both architectures, blueprint mappings, variable validation, YAML escaping", flush=True)
        launcher_checks(work)
        if not args.release_tests:
            return
        for item in metadata["downloads"]:
            download(item, cache)
            print("PASS official archive:", item["architecture"], flush=True)
        output = args.packages_dir.resolve() if args.packages_dir else work / "packages"
        output.mkdir()
        handler = functools.partial(CacheHandler, directory=str(cache))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_port)
            for item in metadata["downloads"]:
                arch = item["architecture"]
                result = json.loads(run(str(helper), "pack", str(ROOT), arch, base, str(output / arch), capture_output=True).stdout)
                print("PASS pier-pkg pack/unpack:", arch, result["sha256"], flush=True)
                next_env = dict(os.environ, PIER_CHECK_NEXT_RELEASE="1")
                run(str(helper), "pack", str(ROOT), arch, base, str(output / arch / "next"), env=next_env, capture_output=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        if args.packages_dir:
            print("Test packages:", output, flush=True)


if __name__ == "__main__":
    main()
