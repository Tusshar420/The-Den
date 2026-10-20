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
import re

import uvicorn

import db

from api import app, check_bind_is_safe
import mailer
import restart_control
import titles
from alexa import notifier as alexa_notifier
import worker
from den_modules import manager as module_manager

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("the-den.main")


class _RedactOAuthParams(logging.Filter):
    """uvicorn logs each request line; the OAuth redirect carries a one-time
    code and state in its query string - keep those out of the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r"([?&](?:code|state)=)[^&\s\"]+", r"\1[redacted]", a) if isinstance(a, str) else a
                                for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactOAuthParams())

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
    check_bind_is_safe(HOST)
    # Before any lane starts: tasks the old process left "running", and the ones a restart held.
    released = restart_control.on_startup()
    mailer.on_started(db._now(), released)
    # Optional modules (modules/): a broken one is isolated and logged; it can never stop The Den starting.
    try:
        module_manager.start_all()
    except Exception:
        log.exception("module startup failed; continuing without modules")
    stop_event = asyncio.Event()

    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)

    api_task = asyncio.create_task(server.serve())
    worker_task = asyncio.create_task(worker.worker_loop(stop_event))
    api_lane_task = asyncio.create_task(worker.api_worker_loop(stop_event))
    assistant_task = asyncio.create_task(worker.assistant_worker_loop(stop_event))
    scheduler_task = asyncio.create_task(worker.scheduler_loop(stop_event))
    namer_task = asyncio.create_task(titles.namer_loop(stop_event))
    run_now_task = asyncio.create_task(worker.run_now_loop(stop_event))
    alexa_task = asyncio.create_task(alexa_notifier.notifier_loop(stop_event))

    log.info("The Den running: API on http://%s:%s , worker + proceed-now + Octopus api lane + assistant lane + scheduler + namer + Alexa notifier active", HOST, PORT)

    try:
        await asyncio.gather(api_task, worker_task, api_lane_task, assistant_task, scheduler_task, namer_task,
                             run_now_task, alexa_task)
    except asyncio.CancelledError:
        pass
    finally:
        stop_event.set()
        server.should_exit = True
        try:
            module_manager.stop_all()
        except Exception:
            log.exception("module shutdown failed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
