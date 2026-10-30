"""app-ci's ClamAV pass: .clamav-allow.sfp loads as a ClamAV database (one that
fails to load fails every app's pass), and the step runs as bash over an image
export served by a stub `docker`."""
from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ALLOW = ROOT / ".clamav-allow.sfp"
ENTRY = re.compile(r"[0-9a-f]{64}:[1-9][0-9]*:[^:\s]+")
DEEP = "/".join(f"d{n}" for n in range(20)) + "/deep.js"

# Stub docker: `export` writes $STUB_TAR (fails when it is empty), `run` maps its
# -v mounts back to the host, logs each listed file with its list, and answers
# FOUND for a file holding EICAR.
DOCKER = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args[0] == "create":
    print("cid")
elif args[0] == "export":
    data = open(os.environ["STUB_TAR"], "rb").read()
    if not data:
        sys.exit("export failed")
    sys.stdout.buffer.write(data)
elif args[0] == "run":
    mounts = {}
    for i, arg in enumerate(args):
        if arg == "-v":
            src, dst = args[i + 1].split(":")[:2]
            mounts[dst] = src
    assert "/var/lib/clamav/catena-allow.sfp" in mounts
    listed = next(a for a in args if a.startswith("--file-list=")).split("=", 1)[1]
    found = False
    with open(os.environ["STUB_LOG"], "a") as log:
        for line in open(mounts["/lists"] + listed[len("/lists"):]).read().splitlines():
            host = mounts["/scan"] + line[len("/scan"):]
            log.write(f"{os.path.basename(listed)} {line}\n")
            if b"EICAR" in open(host, "rb").read():
                print(f"{line}: Eicar-Test-Signature FOUND")
                found = True
    sys.exit(1 if found else 0)
'''


def test_every_line_is_a_comment_or_a_sha256_entry():
    lines = ALLOW.read_text(encoding="utf-8").split("\n")
    assert lines[-1] == "", "the file ends with a newline"
    for number, line in enumerate(lines[:-1], 1):
        assert line.startswith("#") or ENTRY.fullmatch(line), f"line {number}: {line!r}"


def _info(name: str, **attrs) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    for key, value in attrs.items():
        setattr(info, key, value)
    return info


def _image_tar(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name in ("dev", "etc"):
            tar.addfile(_info(name, type=tarfile.DIRTYPE, mode=0o755))
        for name, minor in (("dev/null", 3), ("dev/zero", 5)):
            tar.addfile(_info(name, type=tarfile.CHRTYPE, devmajor=1, devminor=minor))
        tar.addfile(_info("lib", type=tarfile.SYMTYPE, linkname="usr/lib"))
        for name, data in files.items():
            tar.addfile(_info(name, size=len(data), mode=0o400), io.BytesIO(data))
    return out.getvalue()


def _run(tmp_path, tar: bytes, processors: int = 3, available_gb: int = 16):
    bin_dir, runner = tmp_path / "bin", tmp_path / "runner"
    bin_dir.mkdir()
    runner.mkdir()
    (bin_dir / "docker").write_text(DOCKER)
    (bin_dir / "nproc").write_text(f"#!/bin/sh\necho {processors}\n")
    (bin_dir / "free").write_text(
        "#!/bin/sh\necho '               total        used        free      shared  buff/cache   available'\n"
        f"echo 'Mem:        33554432     1048576     1048576           0     1048576    {available_gb * 1048576}'\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    (runner / "images.txt").write_text("example/app:1\n")
    (tmp_path / "image.tar").write_bytes(tar)
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "app-ci.yml").read_text())
    step = next(s for s in workflow["jobs"]["app"]["steps"] if s.get("name") == "ClamAV over each image's files")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TOOLS": str(ROOT),
           "RUNNER_TEMP": str(runner), "CLAMAV_IMAGE": "clamav/clamav:1.5",
           "STUB_TAR": str(tmp_path / "image.tar"), "STUB_LOG": str(tmp_path / "scanned.log")}
    done = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], env=env,
                          capture_output=True, text=True, check=False)
    log = tmp_path / "scanned.log"
    return done, log.read_text().splitlines() if log.exists() else []


FILES = {"etc/app.conf": b"a=1\n", "usr/lib/x.so": b"\x7fELF", "app/main.js": b"run()\n", DEEP: b"deep\n"}


def test_every_regular_file_is_scanned_once_spread_over_the_scanners(tmp_path):
    done, scanned = _run(tmp_path, _image_tar(FILES))
    assert done.returncode == 0, done.stdout + done.stderr
    assert sorted(line.split(" ", 1)[1] for line in scanned) == sorted(f"/scan/{name}" for name in FILES)
    assert {line.split(" ", 1)[0] for line in scanned} == {"part.aa", "part.ab", "part.ac"}


@pytest.mark.parametrize("available_gb,lists", [(5, 2), (1, 1)])
def test_available_memory_bounds_the_scanners_at_one_per_2_gb(tmp_path, available_gb, lists):
    done, scanned = _run(tmp_path, _image_tar(FILES), processors=8, available_gb=available_gb)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len({line.split(" ", 1)[0] for line in scanned}) == lists
    assert len(scanned) == len(FILES)


def test_a_finding_fails_the_step(tmp_path):
    done, _ = _run(tmp_path, _image_tar({**FILES, "app/bad.js": b"EICAR"}))
    assert done.returncode != 0
    assert "/scan/app/bad.js: Eicar-Test-Signature FOUND" in done.stdout


@pytest.mark.parametrize("tar", [b"", b"not a tar archive" * 64], ids=["export fails", "broken archive"])
def test_an_export_that_is_not_a_whole_image_fails_the_step(tmp_path, tar):
    done, scanned = _run(tmp_path, tar)
    assert done.returncode != 0
    assert scanned == []
