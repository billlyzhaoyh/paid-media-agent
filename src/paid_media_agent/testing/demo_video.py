"""Record the demo page's replay as an MP4, for slides.

One headless Chrome, on its own throwaway profile, opens the page; this script sets the replay's
clock frame by frame over the DevTools protocol and takes a screenshot of each, and ffmpeg joins
them. Because the page draws any moment from the clock alone, the video shows exactly what
playback shows. The browser never uses the developer's own Chrome profile.

Development tooling: it needs Chrome, ffmpeg and the `websockets` package on this machine and is
not part of the tests.

    uv run python -m paid_media_agent.testing.demo_video workspace/out/demo.html docs/media/demo-replay.mp4
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

SIZE = (1280, 800)
FPS = 12
PLAY_SECONDS = 28
HOLD_SECONDS = 3
START_SECONDS = 20
CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
)


def find_chrome() -> str:
    for candidate in (os.environ.get("CHROME", ""), *CHROME_PATHS):
        if candidate and (Path(candidate).exists() or shutil.which(candidate)):
            return candidate
    raise RuntimeError("Chrome was not found; set CHROME to its path")


def _debugger_url(profile: Path) -> str:
    """The page's DevTools address, once the browser has written its port to its profile."""
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        if port_file.exists() and port_file.read_text().strip():
            port = int(port_file.read_text().splitlines()[0])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as reply:
                pages = [t for t in json.loads(reply.read()) if t.get("type") == "page"]
            if pages:
                return str(pages[0]["webSocketDebuggerUrl"])
        time.sleep(0.1)
    raise RuntimeError("headless Chrome did not start")


async def _capture(url: str, page: Path, moments: list[float], frames: Path) -> None:
    import websockets  # a development dependency of this tool only

    async with websockets.connect(url, max_size=None) as socket:
        sent = 0

        async def call(method: str, **params: Any) -> dict[str, Any]:
            nonlocal sent
            sent += 1
            await socket.send(json.dumps({"id": sent, "method": method, "params": params}))
            while True:
                message = json.loads(await socket.recv())
                if message.get("id") == sent:
                    if "error" in message:
                        raise RuntimeError(f"{method}: {message['error']}")
                    result: dict[str, Any] = message.get("result", {})
                    return result

        await call(
            "Emulation.setDeviceMetricsOverride",
            width=SIZE[0], height=SIZE[1], deviceScaleFactor=1, mobile=False,
        )  # fmt: skip
        await call("Page.navigate", url=f"{page.resolve().as_uri()}?video=1&t=0")
        for _ in range(100):  # the page is ready when its replay can be set
            ready = await call(
                "Runtime.evaluate", expression="typeof window.demoRender === 'function'"
            )
            if ready.get("result", {}).get("value") is True:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("the demo page did not load")
        for k, moment in enumerate(moments):
            await call("Runtime.evaluate", expression=f"window.demoRender({moment:.4f})")
            shot = await call("Page.captureScreenshot", format="png")
            (frames / f"frame_{k:04d}.png").write_bytes(base64.b64decode(shot["data"]))


def record(page: Path, out: Path, *, days: int = 56, limit: int | None = None) -> Path:
    """Write the replay of `page` to `out` as an MP4 and return it. `limit` keeps only the
    first frames, to try the tool out."""
    chrome = find_chrome()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found")
    playing = FPS * PLAY_SECONDS
    moments = [days * k / playing for k in range(playing + 1)]
    moments += [float(days)] * (FPS * HOLD_SECONDS)  # hold the last frame
    with tempfile.TemporaryDirectory() as work:
        profile, frames = Path(work) / "profile", Path(work) / "frames"
        frames.mkdir()
        browser = subprocess.Popen(  # noqa: S603 - a fixed browser command, on its own profile
            [
                chrome,
                "--headless=new",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--hide-scrollbars",
                f"--window-size={SIZE[0]},{SIZE[1]}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            asyncio.run(_capture(_debugger_url(profile), page, moments[:limit], frames))
        finally:
            browser.terminate()
            try:
                browser.wait(timeout=10)
            except subprocess.TimeoutExpired:
                browser.kill()
                browser.wait()
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603 - a fixed ffmpeg command over frames this function wrote
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-framerate", str(FPS),
                "-i", str(frames / "frame_%04d.png"),
                "-vf", "fps=24,format=yuv420p",
                "-c:v", "libx264", "-crf", "20", "-movflags", "+faststart",
                str(out),
            ],
            check=True,
        )  # fmt: skip
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    print(record(Path(sys.argv[1]), Path(sys.argv[2])))
