"""End-to-end upload check against a running agent: big file, killed connection, resume, SHA-256.

    python tools/upload_check.py https://localhost:8765 <token> [size_mb]
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time

import httpx

CHUNK = 4 * 1024 * 1024


def main(base: str, token: str, size_mb: int = 500) -> int:
    headers = {"Authorization": f"Bearer {token}", "X-LanDeck": "1"}
    size = size_mb * 1024 * 1024
    src = os.path.join(tempfile.gettempdir(), f"landeck-upload-{size_mb}mb.bin")
    print(f"writing {size_mb} MB of random data to {src}")
    digest = hashlib.sha256()
    with open(src, "wb") as f:
        left = size
        while left:
            block = os.urandom(min(left, 8 * 1024 * 1024))
            digest.update(block)
            f.write(block)
            left -= len(block)
    want = digest.hexdigest()

    with httpx.Client(base_url=base, headers=headers, verify=False, timeout=60) as c:
        uid = c.post("/api/v1/files/upload", json={"name": os.path.basename(src), "size": size}).json()["id"]
        t0 = time.monotonic()
        with open(src, "rb") as f:
            off = 0
            killed = False
            while off < size:
                f.seek(off)
                data = f.read(CHUNK)
                if not killed and off >= size // 2:
                    # send half a chunk then drop the connection
                    killed = True

                    def broken(payload=data):
                        yield payload[: len(payload) // 2]
                        raise ConnectionResetError("simulated drop")

                    try:
                        with httpx.Client(base_url=base, headers=headers, verify=False, timeout=60) as c2:
                            c2.put(f"/api/v1/files/upload/{uid}", params={"offset": off}, content=broken())
                    except Exception as exc:
                        print(f"  dropped connection at {off / 2**20:.0f} MB ({type(exc).__name__})")
                    time.sleep(0.5)
                    off = c.get(f"/api/v1/files/upload/{uid}").json()["received"]
                    print(f"  server says it has {off / 2**20:.1f} MB, resuming from there")
                    continue
                r = c.put(f"/api/v1/files/upload/{uid}", params={"offset": off}, content=data)
                r.raise_for_status()
                off = r.json()["received"]
        took = time.monotonic() - t0
        done = c.post(f"/api/v1/files/upload/{uid}/complete").json()
    os.remove(src)
    ok = done["sha256"] == want
    print(f"uploaded {size_mb} MB in {took:.1f}s ({size_mb / took:.0f} MB/s) -> {done['folder']}\\{done['name']}")
    print(f"sha256 local  {want}\nsha256 server {done['sha256']}\nmatch: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 500))
