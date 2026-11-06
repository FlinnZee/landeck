"""One-click RAM cleaner.

Frees memory the same way Mem Reduct (https://github.com/henrypp/memreduct, GPL-3.0) does:
it asks the Windows kernel to empty process working sets and purge the standby / modified
page lists through NtSetSystemInformation. We call those APIs directly with ctypes, so no
native code is bundled. The agent runs elevated, which these calls require.
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from typing import TYPE_CHECKING, Any

import psutil
from fastapi import APIRouter, Body, Depends, Request

from ..api import ApiError, client_ip, feature_dep

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("landeck.ram")

ntdll = ctypes.WinDLL("ntdll")
ntdll.NtSetSystemInformation.restype = ctypes.c_long
ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
ntdll.RtlAdjustPrivilege.restype = ctypes.c_long
ntdll.RtlAdjustPrivilege.argtypes = [ctypes.c_ulong, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte)]

# SYSTEM_INFORMATION_CLASS values used here
SystemFileCacheInformation = 0x15
SystemMemoryListInformation = 0x50
SystemFileCacheInformationEx = 0x51
SystemCombinePhysicalMemoryInformation = 0x82
SystemRegistryReconciliationInformation = 0x9B

# SYSTEM_MEMORY_LIST_COMMAND values
MemoryEmptyWorkingSets = 2
MemoryFlushModifiedList = 3
MemoryPurgeStandbyList = 4
MemoryPurgeLowPriorityStandbyList = 5

# privilege LUIDs for RtlAdjustPrivilege (values from winnt.h SE_*_PRIVILEGE)
SE_INCREASE_QUOTA_PRIVILEGE = 5
SE_PROFILE_SINGLE_PROCESS_PRIVILEGE = 13

MAXSIZE_T = ctypes.c_size_t(~0).value


class SYSTEM_FILECACHE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("CurrentSize", ctypes.c_size_t),
        ("PeakSize", ctypes.c_size_t),
        ("PageFaultCount", wintypes.ULONG),
        ("MinimumWorkingSet", ctypes.c_size_t),
        ("MaximumWorkingSet", ctypes.c_size_t),
        ("CurrentSizeIncludingTransitionInPages", ctypes.c_size_t),
        ("PeakSizeIncludingTransitionInPages", ctypes.c_size_t),
        ("TransitionRePurposeCount", wintypes.ULONG),
        ("Flags", wintypes.ULONG),
    ]


class MEMORY_COMBINE_INFORMATION_EX(ctypes.Structure):
    _fields_ = [
        ("Handle", ctypes.c_void_p),
        ("PagesCombined", ctypes.c_size_t),
        ("Flags", wintypes.ULONG),
    ]


# REGIONS: id -> (label, needs windows build, runner). Order is the order they run in.
def _set_sysinfo(info_class: int, buffer: Any, length: int) -> int:
    return ntdll.NtSetSystemInformation(info_class, buffer, length)


def _memory_command(command: int) -> int:
    cmd = ctypes.c_int(command)
    return _set_sysinfo(SystemMemoryListInformation, ctypes.byref(cmd), ctypes.sizeof(cmd))


def _empty_working_sets() -> int:
    return _memory_command(MemoryEmptyWorkingSets)


def _flush_modified_list() -> int:
    return _memory_command(MemoryFlushModifiedList)


def _purge_standby_list() -> int:
    return _memory_command(MemoryPurgeStandbyList)


def _purge_low_priority_standby() -> int:
    return _memory_command(MemoryPurgeLowPriorityStandbyList)


def _clear_file_cache() -> int:
    info = SYSTEM_FILECACHE_INFORMATION()
    info.MinimumWorkingSet = MAXSIZE_T
    info.MaximumWorkingSet = MAXSIZE_T
    return _set_sysinfo(SystemFileCacheInformationEx, ctypes.byref(info), ctypes.sizeof(info))


def _combine_memory() -> int:
    info = MEMORY_COMBINE_INFORMATION_EX()
    return _set_sysinfo(SystemCombinePhysicalMemoryInformation, ctypes.byref(info), ctypes.sizeof(info))


def _flush_registry_cache() -> int:
    return _set_sysinfo(SystemRegistryReconciliationInformation, None, 0)


# win_build: the lowest Windows 10/11 build the region needs (0 = any supported Windows)
REGIONS: dict[str, dict[str, Any]] = {
    "working_sets": {"label": "Process working sets", "min_build": 0, "run": _empty_working_sets,
                     "desc": "Trim the memory each program is actively holding."},
    "standby": {"label": "Standby cache", "min_build": 0, "run": _purge_standby_list,
                "desc": "Drop cached file data sitting in the standby list. Frees the most."},
    "low_standby": {"label": "Low-priority standby cache", "min_build": 0, "run": _purge_low_priority_standby,
                    "desc": "Drop the lowest-priority cached pages."},
    "modified": {"label": "Modified page list", "min_build": 0, "run": _flush_modified_list,
                 "desc": "Write out and release modified pages."},
    "file_cache": {"label": "System file cache", "min_build": 0, "run": _clear_file_cache,
                   "desc": "Shrink the system working set / file cache."},
    "combine": {"label": "Combine memory", "min_build": 10240, "run": _combine_memory,
                "desc": "Merge identical pages (Windows 10+)."},
    "registry": {"label": "Registry cache", "min_build": 9600, "run": _flush_registry_cache,
                 "desc": "Reconcile the registry cache (Windows 8.1+)."},
}

# a sensible one-click default (everything except the two that can briefly stutter audio/video)
DEFAULT_REGIONS = ["working_sets", "standby", "low_standby", "file_cache", "combine", "registry"]


def _windows_build() -> int:
    import platform

    try:
        return int(platform.version().split(".")[-1])
    except (ValueError, IndexError):
        return 0


def nt_success(status: int) -> bool:
    # NTSTATUS: the top bit (as a signed 32-bit int, status >= 0) means success/informational
    return ctypes.c_long(status).value >= 0


def enable_privileges() -> bool:
    ok = True
    for priv in (SE_PROFILE_SINGLE_PROCESS_PRIVILEGE, SE_INCREASE_QUOTA_PRIVILEGE):
        enabled = ctypes.c_ubyte(0)
        status = ntdll.RtlAdjustPrivilege(ctypes.c_ulong(priv), ctypes.c_ubyte(1), ctypes.c_ubyte(0),
                                          ctypes.byref(enabled))
        ok = ok and nt_success(status)
    return ok


class RamCleaner:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.build = _windows_build()
        self.privileged = False
        self.calls: list[str] = []  # for tests
        if not dry_run:
            from ..native import is_elevated

            self.privileged = is_elevated() and enable_privileges()

    def available_regions(self) -> list[dict[str, Any]]:
        out = []
        for key, r in REGIONS.items():
            if r["min_build"] and self.build and self.build < r["min_build"]:
                continue
            out.append({"id": key, "label": r["label"], "desc": r["desc"],
                        "default": key in DEFAULT_REGIONS})
        return out

    @staticmethod
    def _mem() -> dict[str, int]:
        vm = psutil.virtual_memory()
        return {"total": vm.total, "available": vm.available, "used": vm.total - vm.available,
                "percent": round(vm.percent, 1)}

    def clean(self, regions: list[str] | None) -> dict[str, Any]:
        wanted = [r for r in (regions or DEFAULT_REGIONS) if r in REGIONS]
        if not wanted:
            raise ApiError(400, "no_regions", "Nothing to clean.")
        if not self.dry_run and not self.privileged:
            raise ApiError(403, "not_elevated",
                           "Cleaning RAM needs administrator rights. Start LanDeck normally (it asks once).")
        before = self._mem()
        ran: list[str] = []
        failed: list[str] = []
        started = time.monotonic()
        for key in wanted:
            region = REGIONS[key]
            if region["min_build"] and self.build and self.build < region["min_build"]:
                continue
            if self.dry_run:
                self.calls.append(key)
                ran.append(key)
                continue
            try:
                status = region["run"]()
            except OSError:
                failed.append(key)
                continue
            if nt_success(status):
                ran.append(key)
            else:
                failed.append(key)
                log.warning("RAM region %s failed: status 0x%08x", key, ctypes.c_ulong(status).value)
        after = self._mem()
        freed = max(0, after["available"] - before["available"])
        return {
            "freed": freed,
            "before": before,
            "after": after,
            "regions": ran,
            "failed": failed,
            "took": round(time.monotonic() - started, 2),
            "ts": time.time(),
        }


def build(ctx: "Context") -> APIRouter:
    cleaner = RamCleaner(ctx.dry_run)
    ctx.services["ram"] = cleaner
    gate = feature_dep(ctx, "ram")
    r = APIRouter()
    last: dict[str, Any] = {}

    @r.get("/memory")
    async def memory(dev: dict = Depends(gate)):
        return {
            **RamCleaner._mem(),
            "can_clean": cleaner.dry_run or cleaner.privileged,
            "regions": cleaner.available_regions(),
            "last": last.get("result"),
        }

    @r.post("/memory/clean")
    async def clean(request: Request, body: dict[str, Any] | None = Body(None), dev: dict = Depends(gate)):
        import asyncio

        regions = (body or {}).get("regions")
        if regions is not None and not isinstance(regions, list):
            raise ApiError(400, "bad_regions", "regions must be a list of ids.")
        result = await asyncio.to_thread(cleaner.clean, regions)
        last["result"] = result
        ctx.audit("ram.clean", f"freed {result['freed']} bytes", device=dev, ip=client_ip(request),
                  regions=",".join(result["regions"]))
        ctx.hub.publish("notify", {"kind": "system", "title": "RAM cleaned",
                                   "body": f"Freed {result['freed'] // (1024 * 1024)} MB"})
        return result

    return r
