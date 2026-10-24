"""Windows desktop launcher: runs the web service locally and opens the browser.

The service only listens on 127.0.0.1 because it has no login of its own.
A tray icon offers "打开网页" and "退出"; a second launch just reopens the page.
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 8080
APP_NAME = "DSM Storage Inspector"
MUTEX_NAME = "Local\\DSMStorageInspectorLauncher"
ERROR_ALREADY_EXISTS = 183


def data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "DSMStorageInspector"
    base.mkdir(parents=True, exist_ok=True)
    return base


def port_file() -> Path:
    return data_dir() / "port"


def setup_logging() -> None:
    # A windowed exe has no console, so uvicorn's output goes to a log file.
    logging.basicConfig(
        filename=data_dir() / "launcher.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def is_healthy(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def pick_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("没有可用的本地端口")


def single_instance() -> bool:
    """Return True if this is the only running launcher."""
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError(0)
    # Keep the handle referenced for the life of the process.
    single_instance.handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def message_box(text: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)
    else:
        print(text, file=sys.stderr)


def open_existing_instance() -> None:
    try:
        port = int(port_file().read_text().strip())
    except (OSError, ValueError):
        port = DEFAULT_PORT
    for _ in range(50):
        if is_healthy(port):
            webbrowser.open(f"http://localhost:{port}/")
            return
        time.sleep(0.2)
    message_box("服务已在运行，但暂时无法访问。请稍后重试，或在托盘图标中退出后重新打开。")


def tray_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=12, fill=(23, 43, 58, 255))
    for x, height in ((12, 22), (27, 34), (42, 44)):
        draw.rectangle((x, 54 - height, x + 10, 54), fill=(46, 155, 120, 255))
    return image


def run_tray(url: str, stop) -> None:
    import pystray

    def open_page(icon=None, item=None):
        webbrowser.open(url)

    def quit_app(icon, item):
        icon.stop()

    icon = pystray.Icon(
        "dsm-storage-inspector",
        tray_image(),
        f"{APP_NAME}\n{url}",
        menu=pystray.Menu(
            pystray.MenuItem("打开网页", open_page, default=True),
            pystray.MenuItem("退出", quit_app),
        ),
    )
    icon.run()
    stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="首选端口，被占用时自动换一个")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--no-tray", action="store_true", help="不显示托盘图标（用于调试，Ctrl+C 退出）")
    args = parser.parse_args()

    setup_logging()
    if not single_instance():
        open_existing_instance()
        return 0

    import uvicorn

    if not getattr(sys, "frozen", False):
        # Running from source: make the repository's app package importable.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.main import app

    port = pick_port(args.port)
    url = f"http://localhost:{port}/"
    port_file().write_text(str(port))
    config = uvicorn.Config(app, host=HOST, port=port, log_config=None, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    for _ in range(100):
        if is_healthy(port):
            break
        if not thread.is_alive():
            message_box(f"服务启动失败，详情见日志：\n{data_dir() / 'launcher.log'}")
            return 1
        time.sleep(0.1)
    logging.info("serving on %s", url)
    if not args.no_browser:
        webbrowser.open(url)

    def stop() -> None:
        server.should_exit = True

    try:
        if args.no_tray:
            while thread.is_alive():
                thread.join(0.5)
        else:
            run_tray(url, stop)
    except KeyboardInterrupt:
        stop()
    thread.join(10)
    try:
        port_file().unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
