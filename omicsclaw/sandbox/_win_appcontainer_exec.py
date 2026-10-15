"""Windows AppContainer + Job Object Win32 launcher (pure stdlib ``ctypes``).

Spawns a child process inside a Windows AppContainer security token
(``PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES``) and attaches it to a Win32
Job Object configured with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` so that
closing the parent or killing the launcher guarantees zero orphan processes.

Pure standard-library module: no logging, no dynamic imports, and no direct
reads of the process environment or ``sys`` argument vector.
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

__all__ = [
    "APPCONTAINER_EXEC_PATH",
    "EXTENDED_STARTUPINFO_PRESENT",
    "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES",
    "build_appcontainer_exec_argv",
    "main",
    "run_in_appcontainer_job",
]

APPCONTAINER_EXEC_PATH = Path(__file__).resolve()

CREATE_SUSPENDED = 0x00000004
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
INFINITE = 0xFFFFFFFF


def build_appcontainer_exec_argv(
    python_executable: str,
    child_argv: Sequence[str],
    *,
    sid_string: str,
    cwd: Path | str,
) -> list[str]:
    """Build the command line invoking this helper under *python_executable*."""
    return [
        python_executable,
        "-B",
        str(APPCONTAINER_EXEC_PATH),
        "--sid",
        sid_string,
        "--cwd",
        str(cwd),
        "--",
        *child_argv,
    ]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", ctypes.c_ulong),
        ("Reserved", ctypes.c_ulong),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_ulong),
        ("dwY", ctypes.c_ulong),
        ("dwXSize", ctypes.c_ulong),
        ("dwYSize", ctypes.c_ulong),
        ("dwXCountChars", ctypes.c_ulong),
        ("dwYCountChars", ctypes.c_ulong),
        ("dwFillAttribute", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("wShowWindow", ctypes.c_ushort),
        ("cbReserved2", ctypes.c_ushort),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_ulong),
        ("dwThreadId", ctypes.c_ulong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def run_in_appcontainer_job(
    command: Sequence[str],
    *,
    sid_string: str,
    cwd: str,
) -> int:
    """Spawn *command* inside an AppContainer token and Kill-On-Job-Close Job Object."""
    if sys.platform != "win32" or not hasattr(ctypes, "windll"):
        raise RuntimeError("AppContainer execution requires Windows (win32)")

    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32

    psid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(ctypes.c_wchar_p(sid_string), ctypes.byref(psid)):
        raise OSError(f"ConvertStringSidToSidW failed for SID {sid_string}")

    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        kernel32.LocalFree(psid)
        raise OSError("CreateJobObjectW failed")

    attr_buf = None
    try:
        ext_limit = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ext_limit.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            job_handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(ext_limit),
            ctypes.sizeof(ext_limit),
        )

        size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attr_buf = ctypes.create_string_buffer(size.value)
        attr_list = ctypes.cast(attr_buf, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(
            attr_list, 1, 0, ctypes.byref(size)
        ):
            raise OSError("InitializeProcThreadAttributeList failed")

        sec_caps = _SECURITY_CAPABILITIES()
        sec_caps.AppContainerSid = psid
        sec_caps.Capabilities = None
        sec_caps.CapabilityCount = 0
        sec_caps.Reserved = 0

        if not kernel32.UpdateProcThreadAttribute(
            attr_list,
            0,
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(sec_caps),
            ctypes.sizeof(sec_caps),
            None,
            None,
        ):
            raise OSError("UpdateProcThreadAttribute(SECURITY_CAPABILITIES) failed")

        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        si.lpAttributeList = attr_list
        pi = _PROCESS_INFORMATION()

        cmd_line = subprocess.list2cmdline(list(command))
        cmd_buf = ctypes.create_unicode_buffer(cmd_line)
        if not kernel32.CreateProcessW(
            None,
            cmd_buf,
            None,
            None,
            True,
            EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED,
            None,
            ctypes.c_wchar_p(cwd),
            ctypes.byref(si),
            ctypes.byref(pi),
        ):
            raise OSError("CreateProcessW in AppContainer failed")

        try:
            kernel32.AssignProcessToJobObject(job_handle, pi.hProcess)
            kernel32.ResumeThread(pi.hThread)
            kernel32.WaitForSingleObject(pi.hProcess, INFINITE)
            exit_code = ctypes.c_ulong(0)
            kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
            return int(exit_code.value)
        finally:
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(pi.hProcess)
    finally:
        if attr_buf is not None:
            kernel32.DeleteProcThreadAttributeList(ctypes.cast(attr_buf, ctypes.c_void_p))
        kernel32.CloseHandle(job_handle)
        kernel32.LocalFree(psid)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for ``python -B /path/to/_win_appcontainer_exec.py``."""
    parser = argparse.ArgumentParser(prog="omicsclaw-win-appcontainer-exec")
    parser.add_argument("--sid", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    cmd = list(ns.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        return 0
    return run_in_appcontainer_job(cmd, sid_string=ns.sid, cwd=ns.cwd)


if __package__ is None:  # pragma: no cover
    raise SystemExit(main())
