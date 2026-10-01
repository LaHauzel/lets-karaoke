"""Captured subprocesses with deadlines and cooperative process-tree cancellation."""
from __future__ import annotations

import os
import signal
import subprocess
import time


class ProcessCancelled(RuntimeError):
    """The caller cancelled an operation, rather than the operation failing."""


def attach_kill_on_close_job(process):
    """Bind a Windows worker tree to this owner's lifetime when pywin32 works.

    A non-inherited Job Object handle dies with the server, including abrupt
    exits that bypass Python finally blocks. Missing pywin32 or an external
    job restriction leaves cooperative taskkill cancellation available.
    """
    if os.name != 'nt':
        return None
    if getattr(process, '_windows_job', None) is not None:
        return process._windows_job
    job = None
    try:
        import win32api, win32con, win32job
        # pywin32 311 expects a string name here; None raises TypeError.
        # An empty string creates an unnamed, non-inherited Job Object.
        job = win32job.CreateJobObject(None, '')
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                                     False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        process._windows_job = job
        return job
    except Exception as exc:
        if job is not None:
            job.Close()
        process._windows_job_error = f'{type(exc).__name__}: {exc}'
        return None


def close_kill_on_close_job(process):
    job = getattr(process, '_windows_job', None)
    if job is not None:
        process._windows_job = None
        job.Close()


def _stop_tree(process):
    if getattr(process, '_windows_job', None) is not None:
        close_kill_on_close_job(process)
        return
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Demucs/SOFA may spawn workers; terminating just their Python parent
        # leaves the workers and their GPU allocations alive on Windows.
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10,
                           check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass  # Still reap the parent if taskkill is unavailable.
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def run_process(cmd, *, cancel=None, timeout=None, check=False, cwd=None, env=None):
    """Return a byte-valued CompletedProcess; cancellation kills child workers.

    communicate() drains both pipes while waiting, so verbose subprocesses
    cannot deadlock. Polling also keeps cancellation responsive during a long
    render or source-separation operation.
    """
    if cancel and cancel():
        raise ProcessCancelled("用户已取消")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=cwd, env=env,
                          creationflags=flags, start_new_session=os.name != 'nt') as process:
        attach_kill_on_close_job(process)
        started = time.monotonic()
        try:
            while True:
                if cancel and cancel():
                    raise ProcessCancelled("用户已取消")
                remaining = None if timeout is None else timeout - (time.monotonic() - started)
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(.2, remaining) if remaining is not None else .2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            _stop_tree(process)
            process.communicate()
            raise
        finally:
            close_kill_on_close_job(process)
        if cancel and cancel():
            raise ProcessCancelled("用户已取消")
        result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
