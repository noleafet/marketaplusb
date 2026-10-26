from django.core.management.base import BaseCommand

from apps.jobs.runner import run_worker


class Command(BaseCommand):
    help = "Run a worker that claims and executes queued jobs."

    def add_arguments(self, parser):
        parser.add_argument("--poll", type=float, default=2.0, help="Idle poll seconds.")
        parser.add_argument(
            "--max-jobs", type=int, default=0, help="Exit after N jobs (0 = forever)."
        )
        parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")

    def handle(self, *args, **options):
        max_jobs = 1 if options["once"] else (options["max_jobs"] or None)
        processed = run_worker(poll_seconds=options["poll"], max_jobs=max_jobs)
        self.stdout.write(self.style.SUCCESS(f"Worker processed {processed} job(s)."))
