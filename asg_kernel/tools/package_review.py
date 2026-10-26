"""Export a complete review snapshot, including untracked source and raw tests.

Run: python tools/package_review.py --label round6
Writes only to <repository>/review_artifacts, never commits or pushes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if not args.label.isalnum():
        raise ValueError("Use an alphanumeric snapshot label")
    source = Path(__file__).resolve().parents[1]
    root = source.parent
    destination = root / "review_artifacts" / args.label
    archive = destination.parent / f"ASG_{args.label}_complete.zip"
    if archive.exists():
        raise FileExistsError(f"Refusing to overwrite frozen snapshot: {archive}")
    destination.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest",
               "discover", "-s", "tests", "-t", ".", "-v"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("PYTHONPATH", None)
    test = subprocess.run(command, cwd=source, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=120)
    (destination / "tests.txt").write_bytes(test.stdout)
    if test.returncode:
        raise RuntimeError(f"Tests failed; see {destination / 'tests.txt'}")
    check = subprocess.run(["git", "diff", "--check"], cwd=root, capture_output=True)
    (destination / "diff-check.txt").write_bytes(check.stdout + check.stderr)
    if check.returncode:
        raise RuntimeError("git diff --check failed")
    (destination / "tracked.patch").write_bytes(subprocess.check_output(
        ["git", "diff", "--", "asg_kernel"], cwd=root))
    files = sorted(p for p in source.rglob("*") if p.is_file()
                   and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".sqlite", ".db"})
    manifest = {"snapshot": args.label, "python": sys.version, "platform": platform.platform(),
                "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                "test_command": command, "test_exit_code": test.returncode,
                "diff_check_exit_code": check.returncode,
                "files": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(root).as_posix())
        for path in sorted(destination.iterdir()):
            if path.is_file():
                bundle.write(path, "verification/" + path.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(archive), "files": len(files), "sha256": digest}))


if __name__ == "__main__":
    main()
