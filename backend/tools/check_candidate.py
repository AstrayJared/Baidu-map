"""Test prospective Git files in an isolated source export, without staging.

Existing dependencies are reused; ignored data, secrets, ledgers and caches are
not copied. This validates source completeness, not a fresh dependency install.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def export(target):
    target = target.resolve()
    if not target.is_relative_to(ROOT / ".tmp") or target.exists():
        raise ValueError("candidate_requires_new_directory_under_repo_tmp")
    raw = subprocess.check_output(["git", "-c", f"safe.directory={ROOT.as_posix()}", "-C", str(ROOT),
                                   "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    names = sorted(set(n for n in raw.decode().split("\0") if n))
    files = []
    target.mkdir(parents=True)
    for name in names:
        source = ROOT / name
        if not source.is_file():  # honor existing worktree deletions
            continue
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise ValueError("candidate_source_not_regular_repo_file")
        if source.name.startswith(".env") and source.name != ".env.example":
            raise ValueError("candidate_contains_environment_secrets")
        if any(part in (".hybrid-ledgers", ".poi-ledgers", ".venv", "node_modules", "__pycache__", ".git") for part in source.parts):
            raise ValueError("candidate_contains_ignored_runtime_data")
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        files.append(dict(path=name, sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
    (target.parent / (target.name + "-manifest.json")).write_text(json.dumps(files, indent=2), encoding="utf-8")
    return target


def check(target):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(target / "backend"), str(target / "life-circle-algorithm/src"))))
    checks = []
    # Verify that editable installs cannot silently substitute original source.
    assertion = ("import app,life_circle; from pathlib import Path; "
                 f"root=Path({str(target)!r}); "
                 "assert Path(app.__file__).resolve().is_relative_to(root); "
                 "assert Path(life_circle.__file__).resolve().is_relative_to(root)")
    subprocess.run([sys.executable, "-c", assertion], cwd=target, env=env, check=True)
    for folder in ("backend", "life-circle-algorithm"):
        run = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=target / folder,
                             env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        checks.append(dict(check=folder, returncode=run.returncode, output=run.stdout+run.stderr))
        print(json.dumps(checks[-1], ensure_ascii=True), flush=True)
    dependency_source = ROOT / "life-circle-demo/node_modules"
    link = target / "life-circle-demo/node_modules"
    if not dependency_source.is_dir():
        raise ValueError("frontend_dependencies_unavailable")
    if os.name == "nt":
        # Both paths are explicit; no deletion/move or shell interpolation.
        def quote(p):
            return "'" + str(p).replace("'", "''") + "'"
        subprocess.run(["powershell", "-NoProfile", "-Command",
            f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(dependency_source)} | Out-Null"], check=True)
    else:
        link.symlink_to(dependency_source, target_is_directory=True)
    node = shutil.which("node")
    if node is None:
        raise ValueError("node_unavailable")
    for name, args in (("frontend_tests", ["vitest/vitest.mjs", "run", "--reporter=dot"]),
                       ("frontend_types", ["typescript/bin/tsc", "-b"]),
                       ("frontend_build", ["vite/bin/vite.js", "build"])):
        run = subprocess.run([node, str(link / args[0]), *args[1:]], cwd=link.parent,
                             env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        checks.append(dict(check=name, returncode=run.returncode, output=run.stdout+run.stderr))
        print(json.dumps(checks[-1], ensure_ascii=True), flush=True)
    report = target.parent / (target.name + "-checks.json")
    report.write_text(json.dumps(checks, indent=2, ensure_ascii=False), encoding="utf-8")
    return all(c["returncode"] == 0 for c in checks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if check(export(args.output)) else 1)
