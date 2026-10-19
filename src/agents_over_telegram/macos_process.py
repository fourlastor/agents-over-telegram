"""Read-only Darwin process inspection; never expose raw process environments.

The ABI constants/layout below are from the macOS SDK's libproc.h,
sys/proc_info.h and sys/sysctl.h. Libraries are loaded only on Darwin.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import struct


class _BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        'nfiles', 'pgid', 'jobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
                 ('start_usec', ctypes.c_uint64)]


ROUTING_KEYS = {b'HOME', b'CODEX_HOME', b'CODEX_CLI_PATH',
                b'CODEX_APP_SERVER_WS_URL', b'CODEX_APP_SERVER_USE_LOCAL_DAEMON'}


def parse_procargs(raw):
    """KERN_PROCARGS2: native argc, executable, padding, argv, environment.

    Return argv and only routing fields. Other environment values are discarded
    without decoding, logging or copying them into the returned metadata.
    """
    if len(raw) < 5:
        raise ValueError('invalid_process_arguments')
    argc = struct.unpack_from('=i', raw)[0]
    if not 1 <= argc <= 65536:
        raise ValueError('invalid_process_arguments')
    offset = raw.index(b'\0', 4) + 1
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    args = []
    for _ in range(argc):
        end = raw.index(b'\0', offset)
        args.append(raw[offset:end])
        offset = end + 1
    routing = []
    for pair in raw[offset:].split(b'\0'):
        key, sep, _ = pair.partition(b'=')
        if sep and key in ROUTING_KEYS:
            # Ambiguous routing must fail closed, rather than depend on order.
            if any(value.startswith(key + b'=') for value in routing):
                raise ValueError('invalid_process_arguments')
            routing.append(pair)
    return args, b'\0'.join(routing)


class MacOSProcesses:
    def __init__(self):
        self.proc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        self.libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        self.proc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                          ctypes.c_void_p, ctypes.c_int]
        self.proc.proc_listpids.restype = ctypes.c_int
        self.proc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                          ctypes.c_void_p, ctypes.c_int]
        self.proc.proc_pidinfo.restype = ctypes.c_int
        self.proc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.proc.proc_pidpath.restype = ctypes.c_int
        self.libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                    ctypes.c_void_p, ctypes.c_size_t]
        self.libc.sysctl.restype = ctypes.c_int

    def _sysctl(self, *mib, limit=4 * 1024 * 1024):
        keys = (ctypes.c_int * len(mib))(*mib)
        size = ctypes.c_size_t()
        if self.libc.sysctl(keys, len(mib), None, ctypes.byref(size), None, 0):
            raise OSError('process_inspection_unavailable')
        if not 0 < size.value <= limit:
            raise ValueError('invalid_process_metadata')
        buffer = ctypes.create_string_buffer(size.value)
        if self.libc.sysctl(keys, len(mib), buffer, ctypes.byref(size), None, 0):
            raise OSError('process_inspection_unavailable')
        return buffer.raw[:size.value]

    def _info(self, pid):
        info = _BSDInfo()
        if self.proc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            raise OSError('process_inspection_unavailable')
        if info.pid != pid or info.uid != os.getuid() or info.ruid != os.getuid():
            raise ValueError('process_owner_mismatch')
        return pid, info.ppid, (info.start_sec, info.start_usec)

    def _path(self, pid):
        path = ctypes.create_string_buffer(4096)
        if self.proc.proc_pidpath(pid, path, len(path)) <= 0:
            raise OSError('process_inspection_unavailable')
        return Path(os.fsdecode(path.value)).resolve()

    def group_is_empty(self, pgid):
        # Darwin killpg can report EPERM for a group with no remaining PIDs.
        # A zero result is evidence only if libproc itself reports no error.
        if not isinstance(pgid, int) or pgid <= 1:
            return False
        pids = (ctypes.c_int * 1024)()
        ctypes.set_errno(0)
        count = self.proc.proc_listpids(2, pgid, pids, ctypes.sizeof(pids))
        return count == 0 and ctypes.get_errno() == 0

    def candidates(self, executables):
        needed = self.proc.proc_listpids(1, 0, None, 0)
        if needed <= 0 or needed > 4 * 1024 * 1024:
            raise OSError('process_inspection_unavailable')
        pids = (ctypes.c_int * (needed // ctypes.sizeof(ctypes.c_int) + 1024))()
        count = self.proc.proc_listpids(1, 0, pids, ctypes.sizeof(pids))
        if count <= 0 or count >= ctypes.sizeof(pids):
            raise OSError('process_inventory_incomplete')
        for pid in pids[:count // ctypes.sizeof(ctypes.c_int)]:
            if pid <= 0:
                continue
            try:
                before = self._info(pid)
                executable = self._path(pid)
                if executable not in executables:
                    continue
                # Only inspect arguments/environment of the selected app and CLI.
                args, routing = parse_procargs(self._sysctl(1, 49, pid))
                if self._info(pid) == before and self._path(pid) == executable:
                    yield (*before, executable, args, routing)
            except (OSError, ValueError, UnicodeError, RuntimeError):
                continue
