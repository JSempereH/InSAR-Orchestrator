"""A stand-in for the rclone executable, enough for the backup tests.

Implements only what the app calls (version, listremotes, copy, check, copyto, rcat,
lsf, deletefile) on plain local paths, plus one fake remote called "fakeremote"
whose files live under $FAKE_RCLONE_ROOT. Every invocation is appended to
$FAKE_RCLONE_LOG as one JSON line, so tests can assert on exact arguments.
Set $FAKE_RCLONE_FAIL to a substring to make `copy` fail for matching sources,
and $FAKE_RCLONE_CORRUPT to make `check` fail.
"""
import json
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
log = os.environ.get("FAKE_RCLONE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(args) + "\n")

root = Path(os.environ.get("FAKE_RCLONE_ROOT", "/nonexistent-fake-root"))


def real(path: str) -> Path:
    return root / path.split(":", 1)[1] if path.startswith("fakeremote:") else Path(path)


cmd = args[0]
if cmd == "version":
    print("rclone v1.99.0")
elif cmd == "listremotes":
    print("fakeremote:")
elif cmd == "copy":
    src, dst = real(args[1]), real(args[2])
    fail = os.environ.get("FAKE_RCLONE_FAIL")
    if fail and fail in args[1]:
        print(json.dumps({"level": "error", "msg": "simulated copy failure"}), file=sys.stderr)
        sys.exit(1)
    excludes = [args[i + 1] for i, a in enumerate(args) if a == "--exclude"]
    total = 0
    for f in src.rglob("*"):
        rel = f.relative_to(src)
        if not f.is_file():
            continue
        if any(rel.parts[0] + "/**" == pat for pat in excludes if not pat.startswith(".")):
            continue
        if any(part.startswith(".") for part in rel.parts[:-1]):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        total += f.stat().st_size
    print(json.dumps({"level": "notice", "stats": {"bytes": total, "totalBytes": total, "speed": 1.0, "eta": 0}}), file=sys.stderr)
elif cmd == "check":
    src, dst = real(args[1]), real(args[2])
    bad = [
        str(f.relative_to(src)) for f in src.rglob("*")
        if f.is_file() and (not (dst / f.relative_to(src)).is_file()
                            or (dst / f.relative_to(src)).stat().st_size != f.stat().st_size)
    ]
    if bad or os.environ.get("FAKE_RCLONE_CORRUPT"):
        print(json.dumps({"level": "error", "msg": f"{len(bad)} differences found"}), file=sys.stderr)
        sys.exit(1)
elif cmd == "copyto":
    dst = real(args[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(real(args[1]), dst)
elif cmd == "rcat":
    dst = real(args[1])
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(sys.stdin.buffer.read())
elif cmd == "lsf":
    d = real(args[1])
    if not d.is_dir():
        print("directory not found", file=sys.stderr)
        sys.exit(3)
    for f in sorted(d.iterdir()):
        if f.is_file():
            print(f.name)
elif cmd == "deletefile":
    real(args[1]).unlink()
else:
    print(f"fake rclone: unsupported command {cmd}", file=sys.stderr)
    sys.exit(2)
