"""Start-at-logon via Task Scheduler (elevated, no UAC prompt) plus firewall rules."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .config import CREATE_NO_WINDOW, is_frozen

TASK_NAME = "LanDeck"
FW_RULE = "LanDeck"
FW_RULE_MDNS = "LanDeck mDNS"


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def launch_command() -> tuple[str, str, str]:
    """(exe, arguments, working dir) the task should run."""
    if is_frozen():
        exe = sys.executable
        return exe, "--from-task", str(Path(exe).parent)
    root = Path(__file__).resolve().parent.parent
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else Path(sys.executable))
    return exe, "-m agent.main --from-task", str(root)


def task_xml() -> str:
    exe, args, cwd = launch_command()
    user = escape(current_user())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts the LanDeck agent (phone remote for this PC) when you sign in.</Description>
    <URI>\\{TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
      <Delay>PT10S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(cwd)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, shell=False)


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_elevated(args: list[str], wait: bool = True) -> int:
    """Re-run ourselves through UAC. Returns the child's exit code when waiting."""
    import win32con
    import win32event
    import win32process
    from win32com.shell import shell, shellcon

    if is_frozen():
        exe, params = sys.executable, subprocess.list2cmdline(args)
    else:
        exe = sys.executable
        params = subprocess.list2cmdline(["-m", "agent.main", *args])
    info = shell.ShellExecuteEx(
        fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="runas", lpFile=exe, lpParameters=params,
        lpDirectory=str(Path(__file__).resolve().parent.parent) if not is_frozen() else None,
        nShow=win32con.SW_HIDE)
    handle = info["hProcess"]
    if not wait:
        return 0
    win32event.WaitForSingleObject(handle, win32event.INFINITE)
    return win32process.GetExitCodeProcess(handle)


def task_exists() -> bool:
    return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0


def task_query_xml() -> str | None:
    res = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
    return res.stdout if res.returncode == 0 else None


def task_state() -> str:
    res = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"])
    if res.returncode != 0:
        return "missing"
    for line in res.stdout.splitlines():
        if line.strip().lower().startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "installed"


def install_task() -> None:
    xml = task_xml()
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(path).write_text(xml, encoding="utf-16")
        res = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"])
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout).strip() or "schtasks failed")
    finally:
        Path(path).unlink(missing_ok=True)


def remove_task() -> None:
    if task_exists():
        res = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout).strip() or "schtasks failed")


def run_task() -> bool:
    return _run(["schtasks", "/Run", "/TN", TASK_NAME]).returncode == 0


def install_firewall(port: int) -> None:
    exe, _, _ = launch_command()
    remove_firewall()
    _run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FW_RULE}", "dir=in", "action=allow",
          f"program={exe}", "protocol=TCP", f"localport={port}", "profile=private,domain",
          "description=LanDeck phone remote (LAN only)"])
    _run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FW_RULE_MDNS}", "dir=in", "action=allow",
          f"program={exe}", "protocol=UDP", "localport=5353", "profile=private,domain",
          "description=LanDeck discovery (landeck.local)"])


def remove_firewall() -> None:
    for name in (FW_RULE, FW_RULE_MDNS):
        _run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])


def network_profiles() -> list[dict[str, str]]:
    """Which networks Windows thinks are Public (inbound blocked) vs Private."""
    res = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "Get-NetConnectionProfile | ForEach-Object { $_.InterfaceAlias + '|' + $_.NetworkCategory }"])
    out = []
    for line in res.stdout.splitlines():
        if "|" in line:
            alias, cat = line.strip().split("|", 1)
            out.append({"interface": alias, "category": cat})
    return out
