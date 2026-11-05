from __future__ import annotations

import argparse
import ctypes
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

from . import config as cfg

log = logging.getLogger("landeck")

MUTEX_NAME = "Global\\LanDeckAgent"
ERROR_ALREADY_EXISTS = 183


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="LANDECK", description="Control this PC from your phone over the LAN.")
    p.add_argument("--dev", action="store_true", help="no tray, no elevation, uncached web files")
    p.add_argument("--install", action="store_true", help="start at logon (Task Scheduler) and open the firewall")
    p.add_argument("--uninstall", action="store_true", help="remove the startup task and firewall rules")
    p.add_argument("--purge", action="store_true", help="with --uninstall: also delete settings and paired devices")
    p.add_argument("--status", action="store_true", help="print elevation, task state, port and version")
    p.add_argument("--port", type=int, help="override the configured port")
    p.add_argument("--no-tray", action="store_true")
    p.add_argument("--from-task", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--wait-mutex", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def attach_console() -> None:
    """The packaged exe is a GUI app; borrow the parent console so CLI flags can print."""
    if sys.stdout is not None:
        return
    if ctypes.windll.kernel32.AttachConsole(-1):
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout


def setup_logging(home, dev: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = RotatingFileHandler(home / "landeck.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if dev and sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    for noisy in ("uvicorn.access", "zeroconf", "comtypes", "PIL", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def acquire_mutex(name: str, wait: float = 0) -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    deadline = time.monotonic() + wait
    while True:
        handle = kernel32.CreateMutexW(None, False, name)
        if handle and ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            return handle
        if handle:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def mutex_held(name: str) -> bool:
    SYNCHRONIZE = 0x00100000
    h = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, name)
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def pick_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise SystemExit(f"No free port between {preferred} and {preferred + 19}.")


def self_command(extra: list[str]) -> list[str]:
    if cfg.is_frozen():
        return [sys.executable, *extra]
    return [sys.executable, "-m", "agent.main", *extra]


def cmd_status() -> int:
    from . import startup
    from .native import is_elevated

    conf = cfg.Config(cfg.data_dir()) if cfg.data_dir().exists() else None
    port = conf.port if conf else cfg.DEFAULT_PORT
    print(f"LanDeck {cfg.VERSION}")
    print(f"  this shell elevated : {'yes' if is_elevated() else 'no'}")
    print(f"  agent running       : {'yes' if mutex_held(MUTEX_NAME) else 'no'}")
    print(f"  startup task        : {startup.task_state()}")
    print(f"  port                : {port}")
    print(f"  data folder         : {cfg.data_dir()}")
    return 0


def cmd_install() -> int:
    from . import startup
    from .native import is_elevated

    if not is_elevated():
        print("Asking for administrator rights...")
        return startup.relaunch_elevated(["--install"])
    home = cfg.data_dir()
    home.mkdir(parents=True, exist_ok=True)
    conf = cfg.Config(home)
    try:
        startup.install_task()
    except RuntimeError as exc:
        print(f"Could not create the startup task: {exc}")
        return 1
    startup.install_firewall(conf.port)
    conf.set("autostart", True)
    print(f"Startup task '{startup.TASK_NAME}' installed (runs elevated at logon, no UAC prompt).")
    if not mutex_held(MUTEX_NAME):
        startup.run_task()
        print("Agent started.")
    return 0


def cmd_uninstall(purge: bool) -> int:
    from . import startup
    from .native import is_elevated

    if not is_elevated():
        return startup.relaunch_elevated(["--uninstall"] + (["--purge"] if purge else []))
    try:
        startup.remove_task()
    except RuntimeError as exc:
        print(f"Could not remove the task: {exc}")
    startup.remove_firewall()
    home = cfg.data_dir()
    if home.exists():
        conf = cfg.Config(home)
        conf.set("autostart", False)
    if purge and home.exists():
        shutil.rmtree(home, ignore_errors=True)
        print(f"Removed {home}")
    print("Startup task and firewall rules removed.")
    return 0


def run_agent(args: argparse.Namespace) -> int:
    from .native import MessageWindow, is_elevated, set_dpi_awareness

    set_dpi_awareness()
    mutex_name = MUTEX_NAME + ("-dev" if args.dev else "")
    mutex = acquire_mutex(mutex_name, wait=15 if args.wait_mutex else 0)
    home = cfg.data_dir()
    if mutex is None:
        if not args.from_task:
            port = cfg.Config(home).port if home.exists() else cfg.DEFAULT_PORT
            webbrowser.open(f"https://localhost:{port}/admin")
        return 0

    home.mkdir(parents=True, exist_ok=True)
    setup_logging(home, args.dev)

    import uvicorn

    from .context import Context
    from .server import create_app

    ctx = Context(dev=args.dev)
    port = pick_port(args.port or ctx.config.port)
    if port != ctx.config.port and not args.port:
        log.info("port %s busy, using %s", ctx.config.port, port)
        ctx.config.set("port", port)
    ctx.port = port
    ctx.advertiser.port = port
    ctx.certs.ensure(ctx.ips)
    log.info("LanDeck %s on port %s (elevated=%s, dry_run=%s) ips=%s",
             ctx.version, port, is_elevated(), ctx.dry_run, ctx.ips)

    if not args.dev and cfg.is_frozen() and is_elevated() and ctx.config.get("autostart"):
        from . import startup

        if not startup.task_exists():
            try:
                startup.install_task()
                startup.install_firewall(port)
                log.info("startup task installed")
            except Exception:
                log.exception("could not install startup task")

    app = create_app(ctx)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, ssl_certfile=str(ctx.certs.cert_path),
                            ssl_keyfile=str(ctx.certs.key_path), log_config=None, access_log=False,
                            ws_ping_interval=20, ws_ping_timeout=20, ws_max_size=16 * 1024 * 1024,
                            timeout_graceful_shutdown=3, lifespan="on")
    config.load()
    ctx.ssl_context = config.ssl
    server = uvicorn.Server(config)

    restarting = threading.Event()

    def quit_agent() -> None:
        server.should_exit = True

    def restart_agent() -> None:
        restarting.set()
        server.should_exit = True

    ctx.request_restart = restart_agent

    msgwin = MessageWindow()
    clip = ctx.services.get("clipboard")
    if clip is not None and not ctx.dry_run:
        msgwin.on_clipboard = clip.on_pc_change

    def on_resume() -> None:
        def later():
            for delay in (3, 10, 30):
                time.sleep(delay)
                try:
                    ctx.refresh_network()
                    ctx.advertiser.update(ctx.ips)
                except Exception:
                    log.exception("refresh after resume failed")
        log.info("resumed from sleep")
        threading.Thread(target=later, daemon=True).start()

    msgwin.on_resume = on_resume
    msgwin.on_session_end = quit_agent
    msgwin.start()
    if clip is not None and not ctx.dry_run and not msgwin.clipboard_listener:
        log.info("clipboard listener unavailable, polling instead")
        threading.Thread(target=clip.poll_forever, daemon=True, name="clip-poll").start()

    ctx.advertiser.start(ctx.ips)

    tray = None
    if not args.dev and not args.no_tray:
        from .tray import Tray

        tray = Tray(ctx, f"https://localhost:{port}/admin", quit_agent, restart_agent)
        ctx.on_viewing_changed = tray.set_viewing
        threading.Thread(target=tray.run, daemon=True, name="tray").start()

    try:
        server.run()
    finally:
        ctx.advertiser.stop()
        msgwin.stop()
        if tray:
            tray.stop()
        ctx.audit.close()
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))

    if restarting.is_set():
        extra = ["--wait-mutex"] + (["--dev"] if args.dev else []) + (["--no-tray"] if args.no_tray else [])
        subprocess.Popen(self_command(extra), close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status or args.install or args.uninstall:
        attach_console()
    if args.status:
        return cmd_status()
    if args.install:
        return cmd_install()
    if args.uninstall:
        return cmd_uninstall(args.purge)
    if args.dev:
        os.environ.setdefault("PYTHONUNBUFFERED", "1")
    return run_agent(args)


if __name__ == "__main__":
    sys.exit(main())
