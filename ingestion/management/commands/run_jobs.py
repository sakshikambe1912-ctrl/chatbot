import time

from django.core.management.base import BaseCommand

from ingestion.jobs import requeue_stale, run_next


class Command(BaseCommand):
    help = "Worker: runs queued background jobs. Leave it running, or use --once to run what is queued and exit."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="run the queued jobs, then exit")
        parser.add_argument("--poll", type=float, default=5.0, help="seconds to wait when the queue is empty")

    def handle(self, *args, **opts):
        requeue_stale()
        self.stdout.write("Worker started. Press Ctrl+C to stop.")
        try:
            while True:
                job = run_next()
                if job:
                    self.stdout.write(f"job #{job.id} {job.command}: {job.status}")
                    continue
                if opts["once"]:
                    break
                time.sleep(opts["poll"])
        except KeyboardInterrupt:
            self.stdout.write("Worker stopped.")
