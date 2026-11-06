from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

os.environ["LANDECK_DRY_RUN"] = "1"

from agent.context import Context  # noqa: E402
from agent.server import create_app  # noqa: E402

PHONE_IP = "192.168.1.50"


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.approve = True
        self.prompts: list[tuple[str, str | None]] = []
        self.ctx = Context(home=tmp / "home", dry_run=True, dev=True, confirm_device=self._confirm, discover=False)
        self.ctx.ips = ["192.168.1.10"]
        self.ctx.certs.ensure(self.ctx.ips)
        self.share = tmp / "share"
        self.share.mkdir()
        self.inbox = tmp / "inbox"
        self.ctx.config.set("default_roots", False)
        self.ctx.config.set("show_drives", False)  # keep the test root deterministic
        self.ctx.config.set("roots", [{"name": "Share", "path": str(self.share)}])
        self.ctx.config.set("upload_dir", str(self.inbox))
        self.app = create_app(self.ctx)

    def _confirm(self, name: str, ip: str | None) -> bool:
        self.prompts.append((name, ip))
        return self.approve

    def client(self, ip: str = PHONE_IP, token: str | None = None) -> httpx.AsyncClient:
        headers = {"X-LanDeck": "1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        transport = httpx.ASGITransport(app=self.app, client=(ip, 51000))
        return httpx.AsyncClient(transport=transport, base_url="https://192.168.1.10:8765", headers=headers)

    def local(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 51001))
        return httpx.AsyncClient(transport=transport, base_url="https://localhost:8765", headers={"X-LanDeck": "1"})

    def root_id(self) -> str:
        return self.ctx.services["files"].roots()[0]["id"]

    async def pair(self, name: str = "Test iPhone") -> str:
        async with self.local() as admin:
            res = await admin.post("/admin/api/pair")
            assert res.status_code == 200, res.text
            code = res.json()["code"]
        async with self.client() as phone:
            res = await phone.post("/api/v1/pair", json={"code": code, "name": name})
            assert res.status_code == 200, res.text
            return res.json()["token"]


@pytest.fixture
async def env(tmp_path: Path):
    e = Env(tmp_path)
    e.ctx.hub.attach(asyncio.get_running_loop())
    yield e
    e.ctx.audit.close()


@pytest.fixture
async def token(env: Env) -> str:
    return await env.pair()
