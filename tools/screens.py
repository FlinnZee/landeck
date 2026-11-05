"""Screenshots of every view at iPhone size, for eyeballing layout.

    python tools/screens.py https://localhost:8765 <device-token> [out-dir]

Needs `playwright install chromium` once.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
ROUTES = [
    "/home", "/remote/trackpad", "/remote/keyboard", "/remote/screen", "/files", "/files/{root}",
    "/control/actions", "/control/media", "/control/power", "/more", "/clipboard", "/processes",
    "/apps", "/terminal", "/settings",
]


async def main(base: str, token: str, out: Path, theme: str = "dark") -> int:
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 393, "height": 852}, device_scale_factor=2,
                                        is_mobile=True, has_touch=True, ignore_https_errors=True,
                                        user_agent=IPHONE_UA, color_scheme="dark")
        page = await ctx.new_page()
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        # unpaired screens first
        await page.goto(f"{base}/")
        await page.wait_for_timeout(800)
        await page.screenshot(path=str(out / "00-onboarding.png"))
        await page.goto(f"{base}/pair#not-a-real-secret")
        await page.wait_for_timeout(800)
        await page.screenshot(path=str(out / "01-pair-link.png"))
        await page.evaluate("localStorage.setItem('landeck.prefs', JSON.stringify({onboarded: true}))")
        await page.goto(f"{base}/")
        await page.wait_for_timeout(800)
        await page.screenshot(path=str(out / "02-pair-code.png"))

        prefs = {"onboarded": True, "theme": theme}
        await page.evaluate(f"localStorage.setItem('landeck.token', JSON.stringify({json.dumps(token)}));"
                            f"localStorage.setItem('landeck.prefs', {json.dumps(json.dumps(prefs))})")
        roots = await page.evaluate("""async (t) => {
            const res = await fetch('/api/v1/files/roots', {headers: {Authorization: 'Bearer ' + t, 'X-LanDeck': '1'}});
            return (await res.json()).roots;
        }""", token)
        root_id = roots[0]["id"] if roots else "downloads"
        for i, route in enumerate(ROUTES, start=10):
            r = route.format(root=root_id)
            await page.goto(f"{base}/?v={i}#{r}")
            await page.wait_for_timeout(2200)
            name = f"{i:02d}-{r.strip('/').replace('/', '-') or 'home'}.png"
            await page.screenshot(path=str(out / name))
            print("shot", name)
        await browser.close()
    if errors:
        print("\nconsole:")
        for e in errors:
            print("  ", e)
    return 0


if __name__ == "__main__":
    base, token = sys.argv[1], sys.argv[2]
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(".screens")
    theme = sys.argv[4] if len(sys.argv) > 4 else "dark"
    sys.exit(asyncio.run(main(base, token, out, theme)))
