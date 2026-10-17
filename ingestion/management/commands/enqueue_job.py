import argparse

from django.core.management.base import BaseCommand, CommandError

from ingestion.jobs import ALLOWED, enqueue
from ingestion.models import Job


class Command(BaseCommand):
    help = ("Queue a command to run in the background, e.g.  enqueue_job ingest_docs --store 4  "
            "(a worker started with 'run_jobs' runs it).  enqueue_job --status shows recent jobs.")

    def add_arguments(self, parser):
        parser.add_argument("job_command", nargs="?", help="one of: " + ", ".join(sorted(ALLOWED)))
        parser.add_argument("job_args", nargs=argparse.REMAINDER, help="arguments for that command")
        parser.add_argument("--status", action="store_true", help="show the 15 most recent jobs")

    def handle(self, *args, **opts):
        if opts["status"]:
            for j in Job.objects.all()[:15]:
                note = f"  {j.error[:80]}" if j.error else ""
                self.stdout.write(f"#{j.id:<4} {j.status:<8} {j.command} {' '.join(j.args)} "
                                  f"(attempt {j.attempts}/{j.max_attempts}){note}")
            return
        if not opts["job_command"]:
            raise CommandError("Say which command to queue, for example: enqueue_job ingest_docs --store 4")
        try:
            job = enqueue(opts["job_command"], *opts["job_args"])
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(
            f"Queued job #{job.id}. Start a worker with: python manage.py run_jobs"))
