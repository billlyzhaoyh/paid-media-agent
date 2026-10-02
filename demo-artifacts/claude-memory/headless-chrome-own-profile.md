---
name: headless-chrome-own-profile
description: Never run headless Chrome on the user's default profile for screenshots or frame capture; it glitched their browser
metadata:
  type: feedback
---

Headless Chrome for screenshots or video frames must run on its own throwaway profile, as one instance driven over the DevTools protocol, never as repeated launches on the user's default Chrome profile.

**Why:** On 2026-10-02 hundreds of parallel `--headless=new --screenshot` launches without `--user-data-dir` made the user's open Chrome glitch, and they interrupted the run. With a temporary profile, `--screenshot` writes the file and then never exits, so repeated launches are not a workable alternative.

**How to apply:** Start one `chrome --headless=new --remote-debugging-port=0 --user-data-dir=<tmp>`, read the port from `DevToolsActivePort`, drive it with `websockets` (`Page.navigate`, `Runtime.evaluate`, `Page.captureScreenshot`), and terminate it at the end. `src/paid_media_agent/testing/demo_video.py` in paid-media-agent does this. See [[open-items-live-writes]].
