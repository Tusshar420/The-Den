"""
Entry point: runs the local API and the background worker loop together
in one process.

Run it directly for testing:
    python3 main.py

Or hand it to launchd (see setup/com.tushar.claudequeue.plist) so macOS
keeps it running in the background and restarts it if it ever dies.
"""

import asyncio
import logging
import os

import uvicorn

from api import app
import worker

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("claude-queue.main")

# Bind address:
#   - "127.0.0.1" = only reachable from this Mac itself (fine for local testing)
#   - "0.0.0.0"   = reachable from anywhere that can route to this Mac,
#                   including your Tailscale network. This is what you want
#                   once you're ready to hit it from your phone/tablet.
# Tailscale doesn't require picking its specific IP - binding 0.0.0.0 and
# then only ever calling it via the Mac's *.ts.net / tailnet IP from other
# devices is enough, since nothing outside your tailnet can route to that
# address anyway as long as this port isn't separately forwarded on your
# router.
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))


async def main() -> None:
    stop_event = asyncio.Event()

    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)

    api_task = asyncio.create_task(server.serve())
    worker_task = asyncio.create_task(worker.worker_loop(stop_event))

    log.info("claude-queue running: API on http://%s:%s , worker active", HOST, PORT)

    try:
        await asyncio.gather(api_task, worker_task)
    except asyncio.CancelledError:
        pass
    finally:
        stop_event.set()
        server.should_exit = True


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
