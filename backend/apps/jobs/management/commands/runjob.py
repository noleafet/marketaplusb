import json

from django.core.management.base import BaseCommand, CommandError

from apps.jobs.handlers import HANDLERS
from apps.jobs.models import JobRun
from apps.jobs.runner import JobAlreadyActive, enqueue_job, execute_job, start_inline_job


class Command(BaseCommand):
    help = "Execute a job inline or enqueue it for a worker."

    def add_arguments(self, parser):
        parser.add_argument("job_type", choices=sorted(HANDLERS))
        parser.add_argument("--params", default="{}", help="JSON object with job parameters.")
        parser.add_argument("--enqueue", action="store_true", help="Queue instead of running inline.")

    def handle(self, *args, **options):
        try:
            params = json.loads(options["params"])
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid --params JSON: {exc}") from exc
        if not isinstance(params, dict):
            raise CommandError("--params must be a JSON object")

        try:
            if options["enqueue"]:
                job = enqueue_job(
                    options["job_type"], trigger=JobRun.Trigger.MANUAL, params=params
                )
                self.stdout.write(self.style.SUCCESS(f"Enqueued {job.id}"))
                return
            job = start_inline_job(options["job_type"], params=params)
        except JobAlreadyActive as exc:
            raise CommandError(exc.message) from exc
        job = execute_job(job)
        if job.status == JobRun.Status.SUCCEEDED:
            self.stdout.write(self.style.SUCCESS(json.dumps(job.result, indent=2, default=str)))
        else:
            raise CommandError(f"Job {job.id} failed: {job.error}")
