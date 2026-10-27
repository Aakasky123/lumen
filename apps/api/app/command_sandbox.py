"""A Linux sandbox for the brain's shell commands on the Windows machine (the `run_command` action).

The command runs in WSL inside two nested user namespaces. The outer one (root mapped to the WSL user) has
no network (a fresh, empty network namespace) and its own PIDs, and lays tmpfs over every place the user's
data or Windows is reachable from: /mnt (the Windows drives), /home, /root, /run (WSL's interop sockets,
so a Windows .exe cannot be started), /tmp, /var/tmp. Only a per-run scratch folder is bound back in, at
/mnt/scratch, as the working directory. The command itself runs one namespace deeper, as an unmapped user:
mounts inherited across a user namespace are locked, so it cannot unmount an overlay to reach what lies
beneath, and it cannot mount anything. A wall-clock timeout ends it (the PID namespace dies with it), files
are capped at 100 MB, the environment is scrubbed, and output is capped.

Nothing runs on Windows itself. Before the first command the sandbox proves its own properties with a
probe; if any fails, every command is refused (`sandbox_unavailable`) rather than run unsandboxed.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from typing import Any

OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 60
COMMAND_CHARS = 4000

# The launcher never interpolates the command and never writes into the scratch folder from outside the
# sandbox: the command arrives on stdin and flows through to the innermost bash (`bash -s`), so nothing a
# command plants in scratch (a symlink) can redirect a write made with the WSL user's own rights. $1 is the
# scratch folder name ([A-Za-z0-9_-] only), $2 the timeout in seconds. systemd caps memory (1 GB), tasks
# (256), and CPU (2 cores); output is cut to 16 KB inside WSL, so a command that floods it is stopped by a
# broken pipe instead of filling the API's memory. Run folders older than a day are removed, and a folder
# over 512 MB takes no new command.
_LAUNCHER = r"""
set -u
root="$HOME/.lumen-sandbox"; scratch="$root/$1"; limit="$2"
find "$root" -mindepth 1 -maxdepth 1 -type d -mmin +1440 -exec rm -rf {} + 2>/dev/null
mkdir -p "$scratch" && chmod 700 "$root" "$scratch" || exit 97
used=$(du -sm "$scratch" 2>/dev/null | cut -f1)
[ "${used:-0}" -le 512 ] || { echo "the sandbox scratch folder holds over 512 MB" >&2; exit 96; }
systemd-run --user --scope --quiet -p MemoryMax=1G -p TasksMax=256 -p CPUQuota=200%   env -i PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 HOME=/mnt/scratch TERM=dumb   unshare --user --map-root-user --net --mount --pid --ipc --uts --cgroup --fork --mount-proc --propagation private   /bin/bash -c '
    set -e
    mount -t tmpfs -o size=1m tmpfs /mnt
    mkdir -p /mnt/scratch
    mount --bind "$0" /mnt/scratch
    for hidden in /home /root /run /srv /opt /var/lib /var/crash /dev/shm /dev/mqueue; do
      if [ -d "$hidden" ]; then mount -t tmpfs -o size=1m tmpfs "$hidden"; fi
    done
    mount -t tmpfs -o size=256m tmpfs /tmp
    mount -t tmpfs -o size=64m tmpfs /var/tmp
    cd /mnt/scratch
    exec unshare --user --map-root-user --mount --propagation private /bin/bash -c "ulimit -f 102400; cd /mnt/scratch; exec timeout -k 2 \"\$0\" /bin/bash -s" "$1"
  ' "$scratch" "$limit" 2> >(head -c 16384 >&2) | head -c 16384
exit "${PIPESTATUS[0]}"
"""

# Every line must print PASS for the sandbox to be used.
_PROBE = r"""
test ! -e /mnt/c && echo PASS drives_hidden
test -z "$(ls -A /home)" && echo PASS home_hidden
test ! -e /run/WSL && echo PASS interop_hidden
(exec 3<>/dev/tcp/1.1.1.1/53) 2>/dev/null || echo PASS no_network
(umount /mnt 2>/dev/null || umount /run 2>/dev/null) || echo PASS overlays_locked
(touch /etc/.lumen_probe 2>/dev/null) || echo PASS root_read_only
test -z "$(ls -A /dev/shm)" && echo PASS shm_private
test "$(tail -n +3 /proc/net/dev | grep -cv ' lo:')" = 0 && echo PASS only_loopback
echo ok > probe.txt && test "$(cat probe.txt)" = ok && echo PASS scratch_writable
rm -f probe.txt
"""
_PROBE_CHECKS = (
    "drives_hidden",
    "home_hidden",
    "interop_hidden",
    "no_network",
    "overlays_locked",
    "root_read_only",
    "shm_private",
    "only_loopback",
    "scratch_writable",
)

_PROBE_LOCK = threading.Lock()
_PROBE_RESULT: dict[str, Any] | None = None


def _distro() -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "", os.environ.get("LUMEN_SANDBOX_WSL_DISTRO") or "Ubuntu") or "Ubuntu"


def _scratch_name(run_id: Any) -> str:
    # A run without an id never shares another's folder.
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(run_id or "")) if run_id not in (None, "", 0) else ""
    return "run-" + (cleaned or "adhoc-" + uuid.uuid4().hex[:12])


def _cap(text: str) -> tuple[str, bool]:
    text = str(text or "").replace("\x00", "")
    return (text[:OUTPUT_CHARS], len(text) > OUTPUT_CHARS)


def _launch(command: str, *, scratch: str, timeout_seconds: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["wsl.exe", "-d", _distro(), "-e", "bash", "-c", _LAUNCHER, "lumen", scratch, str(int(timeout_seconds))],
            input=command.encode("utf-8"),
            capture_output=True,
            timeout=timeout_seconds + 20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "failure_reason": "command_timed_out", "duration_ms": int((time.monotonic() - started) * 1000)}
    except (OSError, ValueError) as exc:
        return {"ok": False, "failure_reason": "sandbox_unavailable", "error": str(exc)[:200]}
    stdout, stdout_cut = _cap(completed.stdout.decode("utf-8", "replace"))
    stderr, stderr_cut = _cap(completed.stderr.decode("utf-8", "replace"))
    exit_code = int(completed.returncode)
    return {
        "ok": True,
        "exit_code": exit_code,
        # timeout(1) exits 124 when it ended the command (137 when it had to kill it).
        "timed_out": exit_code in (124, 137),
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_cut,
        "stderr_truncated": stderr_cut,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def probe() -> dict[str, Any]:
    """Prove the sandbox's properties once per process: {"ok": bool, "passed": [...], "failed": [...]}."""
    global _PROBE_RESULT
    with _PROBE_LOCK:
        if _PROBE_RESULT is not None:
            return dict(_PROBE_RESULT)
        result = _launch(_PROBE, scratch="probe", timeout_seconds=20)
        passed = sorted(
            {match for match in re.findall(r"^PASS (\w+)$", str(result.get("stdout") or ""), flags=re.MULTILINE)}
            & set(_PROBE_CHECKS)
        )
        failed = [check for check in _PROBE_CHECKS if check not in passed]
        outcome = {"ok": bool(result.get("ok")) and not failed, "passed": passed, "failed": failed}
        if not result.get("ok"):
            outcome["error"] = str(result.get("failure_reason") or result.get("error") or "")[:200]
        # Cache only a proven sandbox: a failure (WSL still starting) is probed again next time.
        if outcome["ok"]:
            _PROBE_RESULT = dict(outcome)
        return outcome


def run(command: str, *, run_id: Any, timeout_seconds: int | None = None) -> dict[str, Any]:
    """Run one shell command in the sandbox. Never runs it when the sandbox is not proven."""
    command = str(command or "")
    if not command.strip():
        return {"ok": False, "failure_reason": "command_empty"}
    if len(command) > COMMAND_CHARS:
        return {"ok": False, "failure_reason": "command_too_long", "limit": COMMAND_CHARS}
    proven = probe()
    if not proven.get("ok"):
        return {"ok": False, "failure_reason": "sandbox_unavailable", "failed_checks": proven.get("failed") or []}
    limit = max(1, min(MAX_TIMEOUT_SECONDS, int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)))
    result = _launch(command, scratch=_scratch_name(run_id), timeout_seconds=limit)
    return {**result, "sandbox": "wsl", "working_directory": "/mnt/scratch", "timeout_seconds": limit}
