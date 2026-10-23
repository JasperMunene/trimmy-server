"""Background worker entrypoint (the API's is main.py).

    python worker.py                                  # all queues
    python worker.py --queues emails                  # only emails
    python worker.py --queues default --concurrency 20
    python worker.py --beat                           # the periodic-task scheduler (run exactly one)
"""
import argparse
import asyncio
import logging
import sys

from app.core import cache_invalidation  # noqa: F401  (clears cached discovery data on commit)
from app.core.config import settings
from app.workers.broker import QUEUES
from app.workers.beat import Beat
from app.workers.runner import Worker


def main() -> None:
    parser = argparse.ArgumentParser(prog="python worker.py")
    parser.add_argument(
        "--queues",
        default=",".join(QUEUES),
        help=f"comma-separated queues to consume (default: all of {', '.join(QUEUES)})",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=settings.WORKER_CONCURRENCY,
        help="max tasks running at once in this process",
    )
    parser.add_argument(
        "--beat",
        action="store_true",
        help="run the scheduler that enqueues periodic tasks instead of a worker (exactly one per deployment)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    if args.beat:
        asyncio.run(Beat().run())
        return
    queues = [q.strip() for q in args.queues.split(",") if q.strip()]
    asyncio.run(Worker(queues, args.concurrency).run())


if __name__ == "__main__":
    main()
