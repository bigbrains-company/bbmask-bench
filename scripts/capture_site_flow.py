"""Capture frames of the scroll-driven product flow on bbmask.co for the README.

The "how it works" scene on the site is pinned while the page scrolls, and the scroll position
drives the animation. This script drives a headless Chrome through the scene, saves one frame
per requested scroll position and joins the frames into an animated GIF.

    pip install websockets pillow
    python scripts/capture_site_flow.py --lang en
    python scripts/capture_site_flow.py --lang tr

Requires Google Chrome. Not needed to run the benchmark; only to refresh README images.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets
from PIL import Image

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
)
SITE = {"tr": "https://bbmask.co/", "en": "https://bbmask.co/en/"}

# (file name, section selector, scroll progress through the pinned scene from 0 to 1)
FLOW = ".scene.journey"
DEFAULT_SHOTS = (
    ("flow-1-find", FLOW, 0.08),
    ("flow-2-mask", FLOW, 0.50),
    ("flow-3-model", FLOW, 0.66),
    ("flow-4-restore", FLOW, 0.95),
)

SCROLL_TO = """
async (selector, progress) => {
  document.documentElement.style.scrollBehavior = 'auto';
  const scene = document.querySelector(selector);
  if (!scene) return {error: 'section not found: ' + selector};
  const top = scene.getBoundingClientRect().top + scrollY;
  const range = Math.max(0, scene.offsetHeight - innerHeight);
  const target = top + range * progress;
  const start = scrollY;
  // Scroll in small steps: the scene reacts to scroll events, not to the final position.
  for (let i = 1; i <= 30; i++) {
    window.scrollTo(0, start + (target - start) * i / 30);
    await new Promise(r => setTimeout(r, 40));
  }
  await new Promise(r => setTimeout(r, 1800));
  return {y: Math.round(scrollY), range: Math.round(range)};
}
"""


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        path = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if path:
            return path
    raise SystemExit("Google Chrome was not found.")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Page:
    """A minimal Chrome DevTools Protocol client for one tab."""

    def __init__(self, connection):
        self.connection = connection
        self.next_id = 0

    async def call(self, method: str, **params):
        self.next_id += 1
        message_id = self.next_id
        await self.connection.send(
            json.dumps({"id": message_id, "method": method, "params": params})
        )
        while True:
            message = json.loads(await self.connection.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    async def evaluate(self, expression: str):
        result = await self.call(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        return result.get("result", {}).get("value")


async def capture(args: argparse.Namespace) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    port = free_port()
    profile = tempfile.mkdtemp(prefix="bbmask-capture-")
    chrome = subprocess.Popen(
        [
            find_chrome(),
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        targets = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as response:
                    targets = json.load(response)
                break
            except OSError:
                time.sleep(0.2)
        if not targets:
            raise SystemExit("Chrome did not start.")
        socket_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")

        async with websockets.connect(socket_url, max_size=None) as connection:
            page = Page(connection)
            await page.call(
                "Emulation.setDeviceMetricsOverride",
                width=args.width,
                height=args.height,
                deviceScaleFactor=args.scale,
                mobile=False,
            )
            await page.call("Page.enable")
            await page.call("Page.navigate", url=SITE[args.lang])
            for _ in range(60):
                if await page.evaluate("document.readyState") == "complete":
                    break
                await asyncio.sleep(0.25)
            await page.evaluate("document.fonts.ready.then(() => true)")
            await asyncio.sleep(1.5)

            frames = []
            for name, selector, progress in args.shots:
                where = await page.evaluate(f"({SCROLL_TO})({json.dumps(selector)}, {progress})")
                shot = await page.call("Page.captureScreenshot", format="png")
                frame = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")
                frames.append(frame)
                path = out / f"{name}.{args.lang}.png"
                save_still(frame, path, args.still_width)
                print(f"{path}  progress={progress}  {where}")
            if args.gif and len(frames) > 1:
                path = out / f"{args.gif}.{args.lang}.gif"
                save_gif(frames, path, args.gif_width)
                print(path)
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)


def _scaled(frame: Image.Image, width: int) -> Image.Image:
    height = round(frame.height * width / frame.width)
    return frame.resize((width, height), Image.Resampling.LANCZOS)


def save_still(frame: Image.Image, path: Path, width: int) -> None:
    """The page uses a handful of flat colours, so a palette PNG is small and looks the same."""
    still = _scaled(frame, width).quantize(colors=128, dither=Image.Dither.NONE)
    still.save(path, optimize=True)


def save_gif(frames: list[Image.Image], path: Path, width: int) -> None:
    scaled = [_scaled(frame, width) for frame in frames]
    # One palette for all frames, so colours do not shift between them.
    strip = Image.new("RGB", (scaled[0].width, sum(f.height for f in scaled)))
    offset = 0
    for frame in scaled:
        strip.paste(frame, (0, offset))
        offset += frame.height
    palette = strip.quantize(colors=96, dither=Image.Dither.NONE)
    indexed = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in scaled]
    durations = [2600] * (len(indexed) - 1) + [3200]
    indexed[0].save(
        path, save_all=True, append_images=indexed[1:], duration=durations, loop=0, optimize=True
    )


def parse_shot(value: str) -> tuple[str, str, float]:
    name, selector, progress = value.split(":")
    return name, selector, float(progress)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lang", choices=sorted(SITE), default="en")
    parser.add_argument("--out", default="assets/site")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--scale", type=float, default=2, help="device pixel ratio")
    parser.add_argument("--still-width", type=int, default=1600, help="width of the saved PNGs")
    parser.add_argument("--gif", default="flow", help="name of the animated GIF; empty to skip")
    parser.add_argument("--gif-width", type=int, default=1120)
    parser.add_argument(
        "--shot",
        dest="shots",
        action="append",
        type=parse_shot,
        metavar="NAME:SELECTOR:PROGRESS",
        help="frame to capture; repeatable (default: the four steps of the flow)",
    )
    args = parser.parse_args()
    args.shots = args.shots or DEFAULT_SHOTS
    asyncio.run(capture(args))


if __name__ == "__main__":
    main()
