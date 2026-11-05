"""Browser smoke test against a running dev agent.

    python tools/smoke_ui.py https://localhost:8799 <token> "<command that restarts the agent>"

Checks: page connects, survives an agent restart without reloading (pill goes amber then green),
and the live screen view delivers frames.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

from playwright.async_api import async_playwright


async def main(base: str, token: str, restart_cmd: str) -> int:
    ok = True
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 393, "height": 852}, ignore_https_errors=True,
                                        is_mobile=True, has_touch=True, bypass_csp=True)
        page = await ctx.new_page()
        failed: list[str] = []
        page.on("response", lambda r: failed.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        await page.goto(f"{base}/?boot#/home")
        await page.evaluate(f"localStorage.setItem('landeck.token', JSON.stringify({json.dumps(token)}));"
                            "localStorage.setItem('landeck.prefs', JSON.stringify({onboarded: true}))")
        await page.goto(f"{base}/?boot2#/home")
        await page.wait_for_function("document.querySelector('.pill')?.dataset.state === 'online'", timeout=10000)
        print("online: yes")
        await page.evaluate("window.__marker = 42")

        # --- reconnect after agent restart -----------------------------------------
        states: list[str] = []
        await page.expose_function("reportState", lambda s: states.append(s))
        await page.evaluate("""() => {
            const pill = document.querySelector('.pill');
            new MutationObserver(() => window.reportState(pill.dataset.state)).observe(pill, {attributes: true});
        }""")
        t0 = time.monotonic()
        subprocess.Popen(restart_cmd, shell=True)
        try:
            await page.wait_for_function("document.querySelector('.pill').dataset.state !== 'online'", timeout=15000)
            await page.wait_for_function("document.querySelector('.pill').dataset.state === 'online'", timeout=30000)
            took = time.monotonic() - t0
            same_page = await page.evaluate("window.__marker === 42")
            print(f"reconnect: states={states} took={took:.1f}s same_page={same_page}")
            ok &= same_page
        except Exception as exc:
            print("reconnect FAILED", exc, states)
            ok = False

        # --- live screen ------------------------------------------------------------
        await page.goto(f"{base}/?s#/remote/screen")
        await page.wait_for_function("document.querySelector('.pill')?.dataset.state === 'online'", timeout=10000)
        await page.wait_for_timeout(800)
        await page.click("text=Start live view")
        await page.wait_for_timeout(5000)
        stats = await page.inner_text(".screen-stats")
        print("screen stats:", stats)
        drawn = await page.evaluate("document.querySelector('.screen-canvas').width")
        print("canvas width:", drawn)
        ok &= drawn > 100
        await page.click("text=Stop")
        await page.wait_for_timeout(500)

        if failed:
            print("failed requests:")
            for f in sorted(set(failed)):
                print("  ", f)
        await browser.close()
    print("RESULT:", "ok" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3])))
