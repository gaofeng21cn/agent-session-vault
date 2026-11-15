#!/usr/bin/env python3
"""Launch an absolute command once; preserve its actual wait() result separately."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess


def write_record(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not Path(command[0]).is_absolute():
        parser.error("an absolute business command is required after --")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.name):
        parser.error("invalid stage name")
    if not args.log_dir.is_absolute() or args.log_dir.resolve() != args.log_dir:
        parser.error("log directory must be absolute and contain no symlinks")
    args.log_dir.mkdir(parents=True, exist_ok=True)
    base = args.log_dir / args.name
    paths = {key: Path(str(base) + suffix) for key, suffix in {
        "launch": ".launch.json", "stdout": ".stdout.json", "stderr": ".stderr.log",
        "pid": ".pid", "exit_code": ".exit_code", "supervisor": ".supervisor.json",
    }.items()}
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        print(json.dumps({"status": "refused", "reason": "stage already reserved; read existing evidence"}))
        return 75
    try:
        write_record(paths["launch"], {"command": command, "workdir": os.getcwd(),
                                     "created_at": datetime.now(UTC).isoformat()})
    except FileExistsError:
        print(json.dumps({"status": "refused", "reason": "stage already reserved"}))
        return 75
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child:
        os.close(write_fd)
        handshake = os.read(read_fd, 4096).decode(errors="replace")
        os.close(read_fd)
        os.waitpid(child, 0)
        if not handshake.startswith("started "):
            print(json.dumps({"status": "launch_unknown", "reason": handshake or "no handshake",
                              "log_dir": str(args.log_dir)}))
            return 1
        print(json.dumps({"status": "started", "runner_pid": int(handshake.split()[1]),
                          "log_dir": str(args.log_dir),
                          **{key: str(value) for key, value in paths.items()}}))
        return 0
    os.close(read_fd)
    os.setsid()
    if os.fork():
        os._exit(0)
    null_fd = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null_fd, fd)
    if null_fd > 2:
        os.close(null_fd)
    handshake_open = True
    try:
        with paths["stdout"].open("x") as output, paths["stderr"].open("x") as error:
            process = subprocess.Popen(command, stdout=output, stderr=error, stdin=subprocess.DEVNULL)
            with paths["pid"].open("x") as pid_file:
                pid_file.write(f"{process.pid}\n")
            os.write(write_fd, f"started {process.pid}".encode())
            os.close(write_fd)
            handshake_open = False
            code = process.wait()
        # Only wait() may supply this value. Supervisor/start errors are not a
        # business exit code, and must never manufacture a terminal 127.
        temporary = Path(str(base) + ".exit_code.pending")
        with temporary.open("x") as output:
            output.write(f"{code}\n")
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, paths["exit_code"])
        temporary.unlink()
    except BaseException as exc:
        try:
            write_record(paths["supervisor"], {"status": "unknown", "error": f"{type(exc).__name__}: {exc}",
                                               "finished_at": datetime.now(UTC).isoformat()})
            if handshake_open:
                os.write(write_fd, f"error {type(exc).__name__}: {exc}".encode()[:4000])
                os.close(write_fd)
        finally:
            os._exit(1)
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
