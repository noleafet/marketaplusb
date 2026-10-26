import logging
import threading

from django.core.management.base import BaseCommand

from apps.jobs.runner import enqueue_due_schedules, run_worker
from apps.jobs.schedules import ensure_schedules

logger = logging.getLogger("jobs")


class Command(BaseCommand):
    help = "Run the scheduler loop and the job worker together in one process."

    def add_arguments(self, parser):
        parser.add_argument(
            "--scheduler-poll", type=float, default=30.0, help="Schedule check seconds."
        )
        parser.add_argument("--worker-poll", type=float, default=2.0, help="Idle poll seconds.")

    def handle(self, *args, **options):
        ensure_schedules()
        stop = threading.Event()

        def scheduler_loop() -> None:
            while not stop.is_set():
                try:
                    enqueue_due_schedules()
                except Exception:  # noqa: BLE001 - keep running across DB hiccups
                    logger.exception("Scheduler tick failed")
                stop.wait(options["scheduler_poll"])

        scheduler = threading.Thread(target=scheduler_loop, name="scheduler", daemon=True)
        scheduler.start()
        try:
            run_worker(poll_seconds=options["worker_poll"])
        finally:
            stop.set()
            scheduler.join(timeout=5)
