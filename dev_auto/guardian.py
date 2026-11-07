"""Hold the repository lock until the process group is stopped, even after SIGKILL of controller."""
import os
import ctypes
import select
import signal
import subprocess
import sys
import time


def identity(pid):
    if sys.platform != "darwin":
        return None
    info = ctypes.create_string_buffer(136)  # struct proc_bsdinfo, macOS SDK sys/proc_info.h
    lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    size = lib.proc_pidinfo(pid, 3, 0, info, ctypes.sizeof(info))
    if size != 136:
        return None
    if int.from_bytes(info.raw[4:8], sys.byteorder) == 5:  # SZOMB: cannot execute code
        return None
    return info.raw[120:136]  # start time; protects against PID reuse


def descendants(pid):
    """macOS libproc: include tool processes that create their own sessions."""
    if sys.platform != "darwin":
        return {}
    lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    pending, found = [pid], {}
    while pending:
        parent = pending.pop()
        slots = (ctypes.c_int * 16384)()
        size = lib.proc_listchildpids(parent, slots, ctypes.sizeof(slots))
        for child in list(slots)[:max(0, size)]:
            if child > 1 and child not in found:
                found[child] = identity(child)
                pending.append(child)
    return found


def stop_group(child, known):
    known.update(descendants(child.pid))
    for sig, delay in [(signal.SIGTERM, 1.0), (signal.SIGKILL, 0.2)]:
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        for pid, started in known.items():
            if started is None or identity(pid) != started:
                continue
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(delay)
    child.wait()
    # A stuck tool keeps the guardian and repository lock alive (fail closed).
    while any(started is not None and identity(pid) == started for pid, started in known.items()):
        time.sleep(0.2)


def main():
    lock_fds, life_fd = [int(value) for value in sys.argv[1].split(",")], int(sys.argv[2])
    # The inherited lock_fd intentionally stays open until every child is stopped.
    child = subprocess.Popen(sys.argv[3:], start_new_session=True)
    known = {}
    try:
        while child.poll() is None:
            known.update(descendants(child.pid))
            ready, _, _ = select.select([life_fd], [], [], 0.2)
            if ready and not os.read(life_fd, 1):
                stop_group(child, known)
                return 130
        result = child.returncode
        # Stop tools/servers accidentally left in the original process group.
        stop_group(child, known)
        return result
    finally:
        if child.poll() is None:
            stop_group(child, known)
        for fd in lock_fds:
            os.close(fd)
        os.close(life_fd)


if __name__ == "__main__":
    sys.exit(main())
