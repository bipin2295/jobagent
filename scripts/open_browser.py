#!/usr/bin/env python3
"""Open a headed Chromium on the container's virtual display, logged into
LinkedIn, and leave it interactive for the operator via noVNC.

The session is saved to data/browser_profiles/linkedin, so once you sign in
here the apply handler reuses those cookies and does not need a login step.

Usage (inside the container):
    python scripts/open_browser.py linkedin https://www.linkedin.com/login
"""

from __future__ import annotations

import asyncio
import sys

from jobagent.apply.browser import new_page


async def main() -> None:
    family = sys.argv[1] if len(sys.argv) > 1 else "linkedin"
    url = sys.argv[2] if len(sys.argv) > 2 else "https://www.linkedin.com/login"

    print(f"Opening {family} browser at {url}", flush=True)
    print("View it at http://localhost:6080/vnc.html", flush=True)
    print("Sign in, then close the window or press Ctrl-C here.", flush=True)

    async with new_page(family) as page:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        print(f"Loaded: {page.url}", flush=True)
        # Hold the browser open until the operator stops this process.
        while True:
            await asyncio.sleep(5)
            try:
                pages = page.context.pages
                if not pages:
                    print("All tabs closed — exiting.", flush=True)
                    return
                if all(p.is_closed() for p in pages):
                    print("Browser closed — exiting.", flush=True)
                    return
            except Exception:
                print("Browser gone — exiting.", flush=True)
                return


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
