#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_MD = REPO_ROOT / "AGENTS.md"
VENV_BIN = REPO_ROOT / ".venv" / "bin"

STAGE_COMMIT = "commit"
STAGE_PUSH = "push"
STAGE_ORDER = (STAGE_COMMIT, STAGE_PUSH)

DOC_AREA_MAP = (
    (("voice_agent/pipeline/",), "docs/MOVED.md"),
    (("examples/",), "docs/EXAMPLES.md"),
)
DOC_SYNC_LINE_THRESHOLD = 80

CLOUD_MARKERS = ("import inference", "inference.STT", "inference.TTS")


@dataclasses.dataclass(frozen=True)
class CheckResult:
    vok: bool
    vdetail: str


@dataclasses.dataclass(frozen=True)
class CheckSpec:
    vname: str
    vstage: str
    vrule: int
    vrun: Callable[[], CheckResult]


def run_argv(vargv: list[str]) -> CheckResult:
    vproc = subprocess.run(vargv, cwd=REPO_ROOT, capture_output=True, text=True)
    if vproc.returncode == 0:
        return CheckResult(True, "")
    return CheckResult(False, (vproc.stdout + vproc.stderr).strip())


def venv_tool(vname: str) -> str:
    vpath = VENV_BIN / vname
    if not vpath.exists():
        raise SystemExit(f"{vpath} is missing. Run: uv sync")
    return str(vpath)


def changed_paths() -> list[str]:
    vproc = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if vproc.returncode != 0:
        return []
    return [vline for vline in vproc.stdout.splitlines() if vline.strip()]


def changed_line_count(vpaths: list[str]) -> int:
    if not vpaths:
        return 0
    vproc = subprocess.run(
        ["git", "diff", "--numstat", "HEAD", "--", *vpaths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    vtotal = 0
    for vline in vproc.stdout.splitlines():
        vfields = vline.split("\t")
        if len(vfields) == 3 and vfields[0].isdigit() and vfields[1].isdigit():
            vtotal += int(vfields[0]) + int(vfields[1])
    return vtotal


def check_ruff() -> CheckResult:
    return run_argv([venv_tool("ruff"), "check", "."])


def check_tests() -> CheckResult:
    return run_argv([venv_tool("pytest"), "-q", "-m", "not integration and not provider"])


def check_lock() -> CheckResult:
    return run_argv(["uv", "lock", "--check"])


def check_no_cloud_endpoints() -> CheckResult:
    vhits: list[str] = []
    vfiles = [*sorted((REPO_ROOT / "examples").rglob("*.py")), *sorted((REPO_ROOT / "flexus_backend/services/voice").rglob("*.py"))]
    for vpath in vfiles:
        if not vpath.exists():
            continue
        vtext = vpath.read_text(encoding="utf-8")
        vhits += [f"{vpath.relative_to(REPO_ROOT)}: {vmarker}" for vmarker in CLOUD_MARKERS if vmarker in vtext]
    if vhits:
        return CheckResult(False, "\n".join(vhits))
    return CheckResult(True, "")


def check_doc_sync() -> CheckResult:
    vchanged = changed_paths()
    vstale: list[str] = []
    for vprefixes, vdoc in DOC_AREA_MAP:
        vtouched = [vpath for vpath in vchanged if vpath.startswith(vprefixes)]
        if not vtouched or vdoc in vchanged:
            continue
        vlines = changed_line_count(vtouched)
        if vlines >= DOC_SYNC_LINE_THRESHOLD:
            vstale.append(f"{', '.join(vtouched)} changed {vlines} lines without updating {vdoc}")
    if vstale:
        return CheckResult(False, "\n".join(vstale))
    return CheckResult(True, "")


CHECKS = (
    CheckSpec("ruff", STAGE_COMMIT, 1, check_ruff),
    CheckSpec("tests", STAGE_COMMIT, 2, check_tests),
    CheckSpec("no-cloud", STAGE_COMMIT, 4, check_no_cloud_endpoints),
    CheckSpec("doc-sync", STAGE_COMMIT, 13, check_doc_sync),
    CheckSpec("uv-lock", STAGE_PUSH, 3, check_lock),
)


def agents_rules() -> dict[int, str]:
    if not AGENTS_MD.exists():
        return {}
    vrules: dict[int, str] = {}
    vnumber = 0
    vbuf: list[str] = []
    for vline in AGENTS_MD.read_text(encoding="utf-8").splitlines():
        vmatch = re.match(r"^(\d+)\.\s+(.*)$", vline)
        if vmatch:
            if vnumber:
                vrules[vnumber] = " ".join(vbuf).strip()
            vnumber = int(vmatch.group(1))
            vbuf = [vmatch.group(2)]
            continue
        if vnumber and vline.startswith((" ", "\t")):
            vbuf.append(vline.strip())
            continue
        if vnumber:
            vrules[vnumber] = " ".join(vbuf).strip()
            vnumber = 0
            vbuf = []
    if vnumber:
        vrules[vnumber] = " ".join(vbuf).strip()
    return vrules


def selected_checks(vstage: str) -> list[CheckSpec]:
    vlimit = STAGE_ORDER.index(vstage)
    return [vcheck for vcheck in CHECKS if STAGE_ORDER.index(vcheck.vstage) <= vlimit]


def main() -> int:
    vparser = argparse.ArgumentParser()
    vparser.add_argument("--stage", choices=STAGE_ORDER, default=STAGE_COMMIT)
    vargs = vparser.parse_args()

    vrules = agents_rules()
    vfailed: list[tuple[CheckSpec, CheckResult]] = []
    for vcheck in selected_checks(vargs.stage):
        vresult = vcheck.vrun()
        print(f"[{'ok  ' if vresult.vok else 'FAIL'}] {vcheck.vname}")
        if not vresult.vok:
            vfailed.append((vcheck, vresult))

    if not vfailed:
        print(f"verify({vargs.stage}): all checks passed")
        return 0

    for vcheck, vresult in vfailed:
        print(f"\n--- {vcheck.vname} ---\n{vresult.vdetail}")
        vrule = vrules.get(vcheck.vrule)
        if vrule:
            print(f"\nAGENTS.md rule {vcheck.vrule}: {vrule}")
    print(f"\nverify({vargs.stage}): {len(vfailed)} check(s) failed. Fix the cause, do not bypass with --no-verify.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
