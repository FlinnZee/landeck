"""File browser over allowlisted folders: list, thumbs, chunked uploads, ranged downloads, zips."""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import io
import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

from ..api import ApiError, client_ip, feature_dep, require_confirm
from ..config import CREATE_NO_WINDOW

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("landeck.files")

CHUNK = 4 * 1024 * 1024
MAX_CHUNK = 8 * 1024 * 1024
MAX_LIST = 5000
THUMB_PX = 200
SANDBOX_CSP = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'"

KNOWN_FOLDERS = {
    "downloads": ("Downloads", "{374DE290-123F-4565-9164-39C4925E467B}"),
    "desktop": ("Desktop", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"),
    "documents": ("Documents", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"),
    "pictures": ("Pictures", "{33E28130-4E1E-4676-835A-98395C3BC3BB}"),
    "videos": ("Videos", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}"),
}

_EXT_KINDS = {
    "image": "jpg jpeg png gif webp bmp tif tiff heic heif avif ico svg",
    "video": "mp4 m4v mov mkv webm avi wmv flv mpg mpeg 3gp ts",
    "audio": "mp3 m4a aac wav flac ogg opus wma aiff",
    "pdf": "pdf",
    "archive": "zip rar 7z tar gz tgz bz2 xz iso cab",
    "code": "py js ts tsx jsx json html htm css scss c h cpp hpp cs java go rs rb php sh ps1 bat cmd "
            "yml yaml toml ini xml sql lua kt swift",
    "text": "txt md log csv tsv rtf cfg conf env srt",
    "doc": "doc docx odt xls xlsx ods ppt pptx odp pages numbers key epub",
    "exe": "exe msi msix appx lnk scr com",
}
KIND_OF = {ext: kind for kind, exts in _EXT_KINDS.items() for ext in exts.split()}
THUMBABLE = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "ico"}
STORE_IN_ZIP = set(_EXT_KINDS["image"].split() + _EXT_KINDS["video"].split() + _EXT_KINDS["audio"].split()
                   + _EXT_KINDS["archive"].split() + ["pdf", "docx", "xlsx", "pptx"])
RUNNABLE = {"exe", "msi", "msix", "appx", "bat", "cmd", "ps1", "vbs", "vbe", "js", "jse", "wsf", "wsh",
            "scr", "com", "lnk", "reg", "hta", "cpl", "jar"}
INLINE_TYPES = ("image/", "video/", "audio/", "application/pdf")
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)
_BAD_CHARS = set('<>:"/\\|?*')

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


def known_folder(guid: str) -> str | None:
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    u = uuid.UUID(guid)
    g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    out = ctypes.c_wchar_p()
    try:
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
    except (AttributeError, OSError):
        return None
    try:
        return out.value if hr == 0 else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def kind_of(name: str, is_dir: bool = False) -> str:
    if is_dir:
        return "dir"
    return KIND_OF.get(name.rsplit(".", 1)[-1].lower() if "." in name else "", "other")


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 255 or name in (".", ".."):
        raise ApiError(400, "bad_name", "That name isn't allowed.")
    if any(c in _BAD_CHARS or ord(c) < 32 for c in name) or name.endswith((" ", ".")) or _RESERVED.match(name):
        raise ApiError(400, "bad_name", "Names can't contain \\ / : * ? \" < > | or be reserved device names.")
    return name


def unique_path(folder: Path, name: str) -> Path:
    target = folder / name
    if not target.exists():
        return target
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    i = 1
    while True:
        candidate = folder / (f"{stem} ({i}).{ext}" if ext else f"{stem} ({i})")
        if not candidate.exists():
            return candidate
        i += 1


def safe_media_type(name: str) -> tuple[str, bool]:
    """(content type, ok to show inline). Anything that could run script is downgraded to text."""
    kind = kind_of(name)
    if kind in ("code", "text"):
        return "text/plain; charset=utf-8", True
    guessed, _ = mimetypes.guess_type(name)
    if name.lower().endswith(".svg"):
        return "image/svg+xml", True
    if guessed and guessed.startswith(INLINE_TYPES):
        return guessed, True
    return guessed or "application/octet-stream", False


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class _ZipPipe(io.RawIOBase):
    """Unseekable sink for ZipFile that hands buffered chunks to the response generator."""

    def __init__(self, q: "queue.Queue[bytes | None]", cancel: threading.Event):
        self.q = q
        self.cancel = cancel
        self.buf = bytearray()
        self.pos = 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:  # type: ignore[override]
        if self.cancel.is_set():
            raise OSError("client went away")
        self.buf += b
        self.pos += len(b)
        if len(self.buf) >= 256 * 1024:
            self.flush_out()
        return len(b)

    def tell(self) -> int:
        return self.pos

    def flush_out(self) -> None:
        if self.buf:
            self.q.put(bytes(self.buf))
            self.buf.clear()


class FileService:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.uploads_dir = ctx.home / "uploads"
        self.thumbs_dir = ctx.home / "thumbs"
        self.uploads_dir.mkdir(exist_ok=True)
        self.thumbs_dir.mkdir(exist_ok=True)
        self._uploads: dict[str, dict[str, Any]] = {}
        self._ulock = threading.Lock()
        self._thumb_sem = asyncio.Semaphore(3)
        self._load_uploads()

    # --- roots & paths ----------------------------------------------------

    def roots(self) -> list[dict[str, Any]]:
        out = []
        if self.ctx.config.get("default_roots", True):
            for rid, (label, guid) in KNOWN_FOLDERS.items():
                path = known_folder(guid) or str(Path.home() / label)
                if os.path.isdir(path):
                    out.append({"id": rid, "name": label, "path": path, "builtin": True})
        for extra in self.ctx.config.get("roots", []) or []:
            path = extra.get("path", "")
            if path and os.path.isdir(path):
                rid = "r-" + hashlib.sha1(os.path.normcase(path).encode()).hexdigest()[:8]
                out.append({"id": rid, "name": extra.get("name") or os.path.basename(path) or path,
                            "path": path, "builtin": False})
        return out

    def root(self, root_id: str) -> dict[str, Any]:
        for r in self.roots():
            if r["id"] == root_id:
                return r
        raise ApiError(404, "unknown_root", "That folder isn't shared with LanDeck.")

    def resolve(self, root_id: str, rel: str = "", must_exist: bool = True) -> Path:
        base = os.path.realpath(self.root(root_id)["path"])
        rel = (rel or "").replace("\\", "/").strip("/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        for p in parts:
            if p == ".." or ":" in p or any(ord(c) < 32 or c in '<>"|?*' for c in p):
                raise ApiError(400, "bad_path", "That path isn't allowed.")
        target = os.path.realpath(os.path.join(base, *parts)) if parts else base
        try:
            inside = os.path.normcase(os.path.commonpath([base, target])) == os.path.normcase(base)
        except ValueError:
            inside = False
        if not inside:
            raise ApiError(403, "outside_root", "That path leads outside the shared folder.")
        if must_exist and not os.path.exists(target):
            raise ApiError(404, "not_found", "That file or folder doesn't exist anymore.")
        return Path(target)

    def rel_of(self, root_id: str, path: Path) -> str:
        base = os.path.realpath(self.root(root_id)["path"])
        rel = os.path.relpath(path, base).replace("\\", "/")
        return "" if rel == "." else rel

    def upload_dir(self) -> Path:
        configured = self.ctx.config.get("upload_dir")
        if configured:
            path = Path(configured)
        else:
            dl = known_folder(KNOWN_FOLDERS["downloads"][1]) or str(Path.home() / "Downloads")
            path = Path(dl) / "LanDeck"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- listing ----------------------------------------------------------

    def links(self, dev_id: str, root_id: str, rel: str, name: str, kind: str) -> dict[str, str]:
        tok = self.ctx.signer.sign(dev_id, "file", {"r": root_id, "p": rel})
        out = {"url": f"/api/v1/dl/{tok}/{quote(name)}"}
        if name.rsplit(".", 1)[-1].lower() in THUMBABLE:
            out["thumb"] = f"/api/v1/thumb/{self.ctx.signer.sign(dev_id, 'thumb', {'r': root_id, 'p': rel})}"
        return out

    def entry(self, dev_id: str, root_id: str, rel: str, de: os.DirEntry) -> dict[str, Any] | None:
        try:
            st = de.stat()
            is_dir = de.is_dir()
        except OSError:
            return None
        attrs = getattr(st, "st_file_attributes", 0)
        item = {
            "name": de.name,
            "path": f"{rel}/{de.name}".lstrip("/"),
            "dir": is_dir,
            "size": 0 if is_dir else st.st_size,
            "mtime": st.st_mtime,
            "kind": kind_of(de.name, is_dir),
            "hidden": bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)) or de.name.startswith("."),
        }
        if not is_dir:
            item.update(self.links(dev_id, root_id, item["path"], de.name, item["kind"]))
        else:
            tok = self.ctx.signer.sign(dev_id, "zip", {"r": root_id, "p": item["path"]})
            item["zip"] = f"/api/v1/dl/{tok}/{quote(de.name)}.zip"
        return item

    def list_dir(self, dev_id: str, root_id: str, rel: str, hidden: bool) -> dict[str, Any]:
        path = self.resolve(root_id, rel)
        if not path.is_dir():
            raise ApiError(400, "not_a_folder", "That's a file, not a folder.")
        rel = self.rel_of(root_id, path)
        items: list[dict[str, Any]] = []
        truncated = False
        try:
            with os.scandir(path) as it:
                for de in it:
                    e = self.entry(dev_id, root_id, rel, de)
                    if e is None or (e["hidden"] and not hidden):
                        continue
                    items.append(e)
                    if len(items) >= MAX_LIST:
                        truncated = True
                        break
        except PermissionError:
            raise ApiError(403, "no_access", "Windows won't let LanDeck read that folder.")
        items.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        try:
            usage = shutil.disk_usage(path)
            disk = {"free": usage.free, "total": usage.total}
        except OSError:
            disk = None
        tok = self.ctx.signer.sign(dev_id, "zip", {"r": root_id, "p": rel})
        zip_name = quote(path.name or self.root(root_id)["name"])
        return {"root": root_id, "path": rel, "items": items, "truncated": truncated, "disk": disk,
                "zip": f"/api/v1/dl/{tok}/{zip_name}.zip"}

    # --- thumbnails -------------------------------------------------------

    def thumbnail(self, path: Path) -> bytes:
        st = path.stat()
        key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
        cached = self.thumbs_dir / f"{key}.jpg"
        if cached.exists():
            return cached.read_bytes()
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            img.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))
            img = ImageOps.exif_transpose(img)
            img.thumbnail((THUMB_PX, THUMB_PX))
            if img.mode not in ("RGB", "L"):
                bg = Image.new("RGB", img.size, (19, 26, 36))
                rgba = img.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=78)
        data = out.getvalue()
        try:
            cached.write_bytes(data)
        except OSError:
            pass
        return data

    def trim_thumb_cache(self, limit: int = 200 * 1024 * 1024) -> None:
        files = sorted(self.thumbs_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > limit:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)

    # --- zip --------------------------------------------------------------

    def zip_iter(self, root_id: str, folder: Path) -> Iterator[bytes]:
        base = os.path.realpath(self.root(root_id)["path"])
        q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=16)
        cancel = threading.Event()

        def inside(p: str) -> bool:
            try:
                return os.path.normcase(os.path.commonpath([base, os.path.realpath(p)])) == os.path.normcase(base)
            except ValueError:
                return False

        def worker() -> None:
            pipe = _ZipPipe(q, cancel)
            try:
                with zipfile.ZipFile(pipe, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1,
                                     allowZip64=True) as zf:
                    stack = [str(folder)]
                    while stack:
                        cur = stack.pop()
                        try:
                            entries = list(os.scandir(cur))
                        except OSError:
                            continue
                        for de in entries:
                            if de.is_symlink() or getattr(de, "is_junction", lambda: False)():
                                continue
                            if not inside(de.path):
                                continue
                            arc = os.path.relpath(de.path, folder).replace("\\", "/")
                            if de.is_dir():
                                stack.append(de.path)
                                continue
                            ext = de.name.rsplit(".", 1)[-1].lower()
                            ctype = zipfile.ZIP_STORED if ext in STORE_IN_ZIP else zipfile.ZIP_DEFLATED
                            try:
                                zf.write(de.path, arc, compress_type=ctype)
                            except (PermissionError, FileNotFoundError):
                                continue
                pipe.flush_out()
            except OSError:
                pass
            except Exception:
                log.exception("zip failed")
            finally:
                q.put(None)

        threading.Thread(target=worker, daemon=True, name="zip").start()
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    return
                yield chunk
        finally:
            cancel.set()
            # unblock the worker if it is waiting on a full queue
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    # --- uploads ----------------------------------------------------------

    def _meta_path(self, upload_id: str) -> Path:
        return self.uploads_dir / f"{upload_id}.json"

    def _part_path(self, upload_id: str) -> Path:
        return self.uploads_dir / f"{upload_id}.part"

    def _load_uploads(self) -> None:
        cutoff = time.time() - 24 * 3600
        for meta in self.uploads_dir.glob("*.json"):
            try:
                info = json.loads(meta.read_text("utf-8"))
                part = self._part_path(info["id"])
                if info["created"] < cutoff or not part.exists():
                    raise ValueError("stale")
                info["received"] = part.stat().st_size
                self._uploads[info["id"]] = info
            except (OSError, ValueError, KeyError):
                meta.unlink(missing_ok=True)
                self._part_path(meta.stem).unlink(missing_ok=True)
        for part in self.uploads_dir.glob("*.part"):
            if part.stem not in self._uploads:
                part.unlink(missing_ok=True)

    def start_upload(self, dev_id: str, name: str, size: int, root_id: str | None, rel: str | None) -> dict:
        name = validate_name(name)
        if size < 0:
            raise ApiError(400, "bad_size", "Size can't be negative.")
        if root_id:
            dest = self.resolve(root_id, rel or "")
            if not dest.is_dir():
                raise ApiError(400, "not_a_folder", "Uploads need a folder as target.")
        else:
            dest = self.upload_dir()
        free = shutil.disk_usage(dest).free
        if size > free:
            raise ApiError(507, "disk_full", "Not enough free space on the PC for that file.")
        info = {"id": uuid.uuid4().hex, "name": name, "size": size, "dest": str(dest), "received": 0,
                "created": time.time(), "device": dev_id}
        self._part_path(info["id"]).touch()
        self._meta_path(info["id"]).write_text(json.dumps(info), "utf-8")
        with self._ulock:
            self._uploads[info["id"]] = info
        return {"id": info["id"], "chunk_size": CHUNK, "received": 0}

    def upload(self, upload_id: str, dev_id: str) -> dict[str, Any]:
        with self._ulock:
            info = self._uploads.get(upload_id)
        if not info or info["device"] != dev_id:
            raise ApiError(404, "unknown_upload", "That upload isn't known (it may have expired).")
        return info

    async def write_chunk(self, info: dict[str, Any], offset: int, request: Request) -> int:
        if offset != info["received"]:
            raise ApiError(409, "offset_mismatch", "Resume from the server's offset.", received=info["received"])
        if info.get("busy"):
            raise ApiError(409, "busy", "A chunk for this upload is already in flight.", received=info["received"])
        info["busy"] = True
        part = self._part_path(info["id"])
        try:
            with open(part, "r+b") as f:
                f.seek(offset)
                written = 0
                buf = bytearray()
                async for piece in request.stream():
                    if not piece:
                        continue
                    written += len(piece)
                    if written > MAX_CHUNK or info["received"] + len(buf) + len(piece) > info["size"]:
                        raise ApiError(413, "chunk_too_large", "Chunk exceeds the declared size.")
                    buf += piece
                    if len(buf) >= 1024 * 1024:
                        await asyncio.to_thread(f.write, bytes(buf))
                        info["received"] += len(buf)
                        buf.clear()
                if buf:
                    await asyncio.to_thread(f.write, bytes(buf))
                    info["received"] += len(buf)
                f.truncate(info["received"])
        except (OSError, ClientDisconnect):
            # client dropped mid-chunk: keep what reached the disk
            pass
        finally:
            info["received"] = min(part.stat().st_size, info["received"]) if part.exists() else 0
            info["busy"] = False
        return info["received"]

    def finish_upload(self, info: dict[str, Any]) -> dict[str, Any]:
        if info["received"] != info["size"]:
            raise ApiError(409, "incomplete", "Upload isn't complete yet.", received=info["received"])
        part = self._part_path(info["id"])
        dest_dir = Path(info["dest"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = unique_path(dest_dir, info["name"])
        digest = file_sha256(part)
        shutil.move(str(part), str(target))
        self._meta_path(info["id"]).unlink(missing_ok=True)
        with self._ulock:
            self._uploads.pop(info["id"], None)
        return {"name": target.name, "folder": str(dest_dir), "size": info["size"], "sha256": digest}

    def cancel_upload(self, info: dict[str, Any]) -> None:
        self._part_path(info["id"]).unlink(missing_ok=True)
        self._meta_path(info["id"]).unlink(missing_ok=True)
        with self._ulock:
            self._uploads.pop(info["id"], None)

    # --- search -----------------------------------------------------------

    def search(self, dev_id: str, root_id: str, start: Path, q: str, cancel: threading.Event,
               limit: int = 300, budget: float = 15.0) -> dict[str, Any]:
        q = q.strip().lower()
        base = os.path.realpath(self.root(root_id)["path"])
        deadline = time.monotonic() + budget
        results: list[dict[str, Any]] = []
        stack = [str(start)]
        scanned = 0
        timed_out = False
        while stack and len(results) < limit:
            if cancel.is_set():
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
            cur = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            rel_dir = os.path.relpath(cur, base).replace("\\", "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            for de in entries:
                scanned += 1
                try:
                    if de.is_symlink() or getattr(de, "is_junction", lambda: False)():
                        continue
                    if de.is_dir():
                        stack.append(de.path)
                except OSError:
                    continue
                if q in de.name.lower():
                    e = self.entry(dev_id, root_id, rel_dir, de)
                    if e:
                        results.append(e)
                        if len(results) >= limit:
                            break
        return {"items": results, "scanned": scanned, "timed_out": timed_out,
                "cancelled": cancel.is_set(), "truncated": len(results) >= limit}


def reveal_in_explorer(path: Path) -> None:
    try:
        from win32com.shell import shell

        folder_pidl = shell.SHILCreateFromPath(str(path.parent), 0)[0]
        item_pidl = shell.SHILCreateFromPath(str(path), 0)[0]
        shell.SHOpenFolderAndSelectItems(folder_pidl, (item_pidl,), 0)
    except Exception:
        subprocess.Popen(["explorer.exe", f"/select,{path}"], creationflags=CREATE_NO_WINDOW)


def build(ctx: "Context") -> APIRouter:
    svc = FileService(ctx)
    ctx.services["files"] = svc
    gate = feature_dep(ctx, "files")
    r = APIRouter()

    async def bg_trim() -> None:
        await asyncio.to_thread(svc.trim_thumb_cache)

    ctx.background.append(bg_trim)

    @r.get("/files/roots")
    async def roots(dev: dict = Depends(gate)):
        out = []
        for root in svc.roots():
            try:
                usage = shutil.disk_usage(root["path"])
                disk = {"free": usage.free, "total": usage.total}
            except OSError:
                disk = None
            out.append({"id": root["id"], "name": root["name"], "path": root["path"], "disk": disk})
        return {"roots": out, "upload_dir": str(svc.upload_dir())}

    @r.get("/files/list")
    async def list_dir(root: str, path: str = "", hidden: int = 0, dev: dict = Depends(gate)):
        return await asyncio.to_thread(svc.list_dir, dev["id"], root, path, bool(hidden))

    @r.get("/files/stat")
    async def stat(root: str, path: str = "", dev: dict = Depends(gate)):
        p = svc.resolve(root, path)
        st = p.stat()
        rel = svc.rel_of(root, p)
        info = {"name": p.name, "path": rel, "dir": p.is_dir(), "size": st.st_size, "mtime": st.st_mtime,
                "kind": kind_of(p.name, p.is_dir())}
        if not p.is_dir():
            info.update(svc.links(dev["id"], root, rel, p.name, info["kind"]))
        return info

    @r.get("/files/search")
    async def search(request: Request, root: str, q: str = Query(..., min_length=1), path: str = "",
                     dev: dict = Depends(gate)):
        start = svc.resolve(root, path)
        cancel = threading.Event()
        job = asyncio.create_task(asyncio.to_thread(svc.search, dev["id"], root, start, q, cancel))
        while not job.done():
            await asyncio.wait({job}, timeout=0.25)
            if not job.done() and await request.is_disconnected():
                cancel.set()
        return job.result()

    @r.post("/files/mkdir")
    async def mkdir(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        parent = svc.resolve(body.get("root", ""), body.get("path", ""))
        target = parent / validate_name(body.get("name", ""))
        if target.exists():
            raise ApiError(409, "exists", "Something with that name already exists.")
        target.mkdir()
        ctx.audit("files.mkdir", target, device=dev, ip=client_ip(request))
        return {"ok": True, "path": svc.rel_of(body["root"], target)}

    @r.post("/files/rename")
    async def rename(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        rel = body.get("path", "")
        if not rel.strip("/"):
            raise ApiError(400, "is_root", "The shared folder itself can't be renamed.")
        src = svc.resolve(body.get("root", ""), rel)
        dst = src.parent / validate_name(body.get("name", ""))
        if dst.exists() and os.path.normcase(str(dst)) != os.path.normcase(str(src)):
            raise ApiError(409, "exists", "Something with that name already exists.")
        src.rename(dst)
        ctx.audit("files.rename", f"{src} -> {dst.name}", device=dev, ip=client_ip(request))
        return {"ok": True, "path": svc.rel_of(body["root"], dst)}

    @r.post("/files/delete")
    async def delete(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        require_confirm(body)
        paths = body.get("paths") or [body.get("path", "")]
        root_id = body.get("root", "")
        targets = []
        for rel in paths:
            if not str(rel).strip("/"):
                raise ApiError(400, "is_root", "The shared folder itself can't be deleted.")
            targets.append(svc.resolve(root_id, rel))

        def trash() -> None:
            if ctx.dry_run:
                return
            from send2trash import send2trash

            for t in targets:
                send2trash(str(t))

        try:
            await asyncio.to_thread(trash)
        except OSError as exc:
            raise ApiError(500, "delete_failed", f"Couldn't move to the Recycle Bin: {exc}")
        for t in targets:
            ctx.audit("files.delete", t, device=dev, ip=client_ip(request))
        return {"ok": True, "count": len(targets)}

    @r.post("/files/open")
    async def open_on_pc(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        target = svc.resolve(body.get("root", ""), body.get("path", ""))
        if target.suffix.lower().lstrip(".") in RUNNABLE:
            require_confirm(body)
        if not ctx.dry_run:
            await asyncio.to_thread(os.startfile, str(target))  # type: ignore[attr-defined]
        ctx.audit("files.open", target, device=dev, ip=client_ip(request))
        return {"ok": True}

    @r.post("/files/reveal")
    async def reveal(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        target = svc.resolve(body.get("root", ""), body.get("path", ""))
        if not ctx.dry_run:
            await asyncio.to_thread(reveal_in_explorer, target)
        ctx.audit("files.reveal", target, device=dev, ip=client_ip(request))
        return {"ok": True}

    # --- uploads ----------------------------------------------------------

    @r.post("/files/upload")
    async def upload_start(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        try:
            size = int(body.get("size", -1))
        except (TypeError, ValueError):
            raise ApiError(400, "bad_size", "size must be a number.")
        res = await asyncio.to_thread(svc.start_upload, dev["id"], str(body.get("name", "")), size,
                                      body.get("root") or None, body.get("path") or None)
        ctx.audit("files.upload_start", f"{body.get('name')} ({size} bytes)", device=dev, ip=client_ip(request))
        return res

    @r.get("/files/upload/{upload_id}")
    async def upload_status(upload_id: str, dev: dict = Depends(gate)):
        info = svc.upload(upload_id, dev["id"])
        return {"id": upload_id, "received": info["received"], "size": info["size"], "chunk_size": CHUNK}

    @r.put("/files/upload/{upload_id}")
    async def upload_chunk(upload_id: str, request: Request, offset: int, dev: dict = Depends(gate)):
        info = svc.upload(upload_id, dev["id"])
        received = await svc.write_chunk(info, offset, request)
        return {"received": received, "size": info["size"]}

    @r.post("/files/upload/{upload_id}/complete")
    async def upload_done(upload_id: str, request: Request, dev: dict = Depends(gate)):
        info = svc.upload(upload_id, dev["id"])
        res = await asyncio.to_thread(svc.finish_upload, info)
        ctx.audit("files.upload", f"{res['folder']}\\{res['name']}", device=dev, ip=client_ip(request))
        center = ctx.services.get("notify")
        if center:
            center.push("file", "Upload complete", f"{res['name']} saved to {res['folder']}")
        ctx.hub.publish("files", {"event": "uploaded", **res})
        return res

    @r.delete("/files/upload/{upload_id}")
    async def upload_cancel(upload_id: str, dev: dict = Depends(gate)):
        info = svc.upload(upload_id, dev["id"])
        await asyncio.to_thread(svc.cancel_upload, info)
        return {"ok": True}

    # --- signed downloads (no bearer header possible from <img>/<video>) ---

    def check_link(token: str, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
        data = ctx.signer.verify(token, kind)
        if not data:
            raise ApiError(403, "link_expired", "This link expired. Refresh the folder.")
        dev = ctx.devices.get(data["d"])
        if not dev:
            raise ApiError(401, "unauthorized", "This device is no longer paired.")
        if not ctx.config.feature("files"):
            raise ApiError(403, "feature_disabled", "File access is turned off on the PC.")
        return data, dev

    @r.get("/dl/{token}/{name}")
    async def download(token: str, name: str, request: Request, dl: int = 0):
        if name.endswith(".zip"):
            data = ctx.signer.verify(token, "zip")
            if data:
                _, dev = check_link(token, "zip")
                folder = svc.resolve(data["r"], data["p"])
                if not folder.is_dir():
                    raise ApiError(400, "not_a_folder", "Only folders can be zipped.")
                ctx.audit("files.download_zip", folder, device=dev, ip=client_ip(request))
                fname = quote(f"{folder.name or 'folder'}.zip")
                return StreamingResponse(svc.zip_iter(data["r"], folder), media_type="application/zip", headers={
                    "content-disposition": f"attachment; filename*=UTF-8''{fname}",
                    "content-security-policy": SANDBOX_CSP,
                })
        data, dev = check_link(token, "file")
        path = svc.resolve(data["r"], data["p"])
        if path.is_dir():
            raise ApiError(400, "is_folder", "Folders download as zip.")
        media_type, inline = safe_media_type(path.name)
        disposition = "inline" if inline and not dl else "attachment"
        if not request.headers.get("range") or request.headers.get("range", "").startswith("bytes=0-"):
            ctx.audit("files.download", path, device=dev, ip=client_ip(request))
        return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type=disposition,
                            headers={"content-security-policy": SANDBOX_CSP,
                                     "cache-control": "private, max-age=300"})

    @r.get("/thumb/{token}")
    async def thumb(token: str):
        data, _ = check_link(token, "thumb")
        path = svc.resolve(data["r"], data["p"])
        async with svc._thumb_sem:
            try:
                jpeg = await asyncio.to_thread(svc.thumbnail, path)
            except Exception:
                raise ApiError(415, "no_thumb", "No preview for this file.")
        return Response(jpeg, media_type="image/jpeg",
                        headers={"cache-control": "private, max-age=86400", "content-security-policy": SANDBOX_CSP})

    return r
