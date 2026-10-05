#!/usr/bin/env python3
"""Run the release runtime checks on AlmaLinux 8/9 and Ubuntu 24.04, amd64/arm64."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BASES = {"almalinux8": "almalinux:8.10", "almalinux9": "almalinux:9.8", "ubuntu2404": "ubuntu:24.04"}
TARGETS = [system + "/" + arch for system in BASES for arch in ["amd64", "arm64"]]


def image_for(system, arch):
    tag = "pier-apps-test:" + system + "-" + arch
    for image in [tag, "pier-agent-test-" + system + ":" + arch]:
        result = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Architecture}}"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip() == arch:
            return image
    subprocess.run([
        "docker", "build", "--platform", "linux/" + arch,
        "--build-arg", "BASE_IMAGE=" + BASES[system],
        "--build-arg", "http_proxy", "--build-arg", "https_proxy", "--build-arg", "no_proxy",
        "-f", str(ROOT / "tests/runtime.Dockerfile"), "-t", tag, str(ROOT / "tests"),
    ], check=True)
    return tag


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", required=True, type=Path, help="output from check.py --release-tests --packages-dir")
    parser.add_argument("--targets", nargs="+", choices=TARGETS, default=TARGETS)
    args = parser.parse_args()
    packages = args.packages.resolve()
    jobs = []
    for target in args.targets:
        system, arch = target.split("/")
        for path in [packages / arch / "unpacked", packages / arch / "next/unpacked"]:
            if not (path / "bin/start.sh").is_file():
                parser.error("missing test package: " + str(path))
        jobs.append((target, arch, image_for(system, arch)))

    def run(job):
        target, arch, image = job
        result = subprocess.run([
            "docker", "run", "--rm", "--platform", "linux/" + arch,
            "--network", "none", "--read-only", "--user", "65534:65534",
            "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777",
            "--mount", "type=bind,src=" + str(packages) + ",dst=/packages,readonly",
            "--mount", "type=bind,src=" + str(ROOT / "tests") + ",dst=/tests,readonly",
            "--entrypoint", "python3", image, "/tests/runtime.py", "/packages/" + arch,
        ], capture_output=True, text=True, timeout=180)
        return target, result

    failures = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for target, result in pool.map(run, jobs):
            print(target + ": " + ("PASS" if result.returncode == 0 else "FAIL"), flush=True)
            print(result.stdout + result.stderr, end="", flush=True)
            if result.returncode:
                failures.append(target)
    if failures:
        raise SystemExit("Failed: " + ", ".join(failures))


if __name__ == "__main__":
    main()
