# Confines one tool call with Landlock before it runs: it may read the system and the agent's data, write only its
# own scratch directory, never list processes, signal outside its own tree, or open TCP connections.
# Run as `python -m astralvessel.confine --scratch DIR --read PATH ... -- argv...`; it applies the rules, then execs.

from __future__ import annotations

import ctypes
import os
import struct
import sys

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
REQUIRED_ABI = 6  # filesystem, TCP and signal/abstract-socket scoping

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1, 2, 4, 8
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | (1 << 14)  # rights that apply to a single file (with truncate)
READ_RIGHTS = EXECUTE | READ_FILE | READ_DIR
NET_BIND_TCP, NET_CONNECT_TCP = 1, 2
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1, 2
SYSTEM_PATHS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")
DEVICES = ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random")

_libc = ctypes.CDLL(None, use_errno=True)


def abi_version() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    version = _libc.syscall(SYS_CREATE_RULESET, None, 0, CREATE_RULESET_VERSION)
    return max(version, 0)


def _fs_rights(abi: int) -> int:
    # Every filesystem right this kernel knows; anything handled and not granted below is denied.
    known = {1: 13, 2: 14, 3: 15, 4: 15, 5: 16}
    return (1 << known.get(abi, 16)) - 1


def apply(scratch: str, readable: list[str]) -> None:
    abi = abi_version()
    if abi < REQUIRED_ABI:
        raise OSError(f"Landlock ABI {REQUIRED_ABI} or newer is required; this kernel offers {abi}")
    fs = _fs_rights(abi)
    attr = struct.pack("QQQ", fs, NET_BIND_TCP | NET_CONNECT_TCP, SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL)
    ruleset = _libc.syscall(SYS_CREATE_RULESET, attr, len(attr), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    try:
        grants = [(p, READ_RIGHTS) for p in (*SYSTEM_PATHS, *readable)]
        grants += [(d, READ_FILE | WRITE_FILE) for d in DEVICES] + [(scratch, fs)]
        for path, rights in grants:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # a path this image lacks needs no rule
            try:
                if not os.path.isdir(path):
                    rights &= FILE_RIGHTS
                rule = struct.pack("=Qi", rights & fs, fd)
                if _libc.syscall(SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH, rule, 0) != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
            finally:
                os.close(fd)
        if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS) failed")
        if _libc.syscall(SYS_RESTRICT_SELF, ruleset, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset)


def main(argv: list[str]) -> None:
    scratch, readable = "", []
    while argv and argv[0] != "--":
        flag, value, argv = argv[0], argv[1], argv[2:]
        if flag == "--scratch":
            scratch = value
        elif flag == "--read":
            readable.append(value)
        else:
            raise SystemExit(f"unknown flag {flag}")
    command = argv[1:]
    if not scratch or not command:
        raise SystemExit("usage: confine --scratch DIR [--read PATH]... -- argv...")
    try:
        apply(scratch, readable)
    except OSError:
        sys.stderr.write("the sandbox could not confine this call\n")
        os._exit(126)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
