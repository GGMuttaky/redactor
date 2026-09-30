# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Make ffmpeg child processes die with the app on Windows.

Closing the console window kills Python but not the ffmpeg processes it started
(they run without a console of their own), so a preview encode would keep using
the CPU and an export would finish a truncated file. Each ffmpeg process is put in
a Windows job object that kills its members when the app's handle to it closes,
which the OS does when the app exits for any reason.

Only ffmpeg/ffprobe are added, never the app's own process: the browser, Explorer
and the report viewer are started by the app too, and must outlive it.
"""
import ctypes
import sys
import threading
from ctypes import wintypes

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9

_job = None
_lock = threading.Lock()


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _get_job():
    global _job
    with _lock:
        if _job is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                                    ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            _job = (kernel32, job)  # the handle is kept open until the app exits
        return _job


def bind_to_app(proc):
    """Kill `proc` (a subprocess.Popen) when the app exits. Best effort: never raises."""
    if sys.platform != "win32":
        return
    try:
        kernel32, job = _get_job()
        kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))
    except (OSError, AttributeError, ValueError):
        pass
