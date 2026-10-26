import logging
import signal
import time

from django.core.management.base import BaseCommand

from apps.jobs.runner import enqueue_due_schedules
from apps.jobs.schedules import ensure_schedules

logger = logging.getLogger("jobs")


class Command(BaseCommand):
    help = "Run the scheduler loop that enqueues due jobs."

    def add_arguments(self, parser):
        parser.add_argument("--poll", type=float, default=30.0, help="Seconds between checks.")
        parser.add_argument("--once", action="store_true", help="Enqueue due jobs once and exit.")

    def handle(self, *args, **options):
        stop = {"flag": False}

        def _stop(signum, frame):  # noqa: ARG001
            stop["flag"] = True

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        created = ensure_schedules()
        if created:
            self.stdout.write(
                self.style.SUCCESS(f"Created {len(created)} default schedule(s).")
            )
        if options["once"]:
            jobs = enqueue_due_schedules()
            self.stdout.write(self.style.SUCCESS(f"Enqueued {len(jobs)} due job(s)."))
            return
        self.stdout.write("Scheduler running. Press Ctrl+C to stop.")
        while not stop["flag"]:
            try:
                jobs = enqueue_due_schedules()
                for job in jobs:
                    self.stdout.write(f"Enqueued {job.job_type} ({job.id})")
            except Exception:  # noqa: BLE001 - keep the scheduler alive across DB hiccups
                logger.exception("Scheduler tick failed")
            deadline = time.monotonic() + options["poll"]
            while not stop["flag"] and time.monotonic() < deadline:
                time.sleep(min(1.0, options["poll"]))
        self.stdout.write("Scheduler stopped.")
