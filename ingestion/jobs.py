"""A small database-backed job queue: no Redis or Celery needed.
Queue a job:   python manage.py enqueue_job ingest_docs --store 4
Run the worker: python manage.py run_jobs
"""
import io
import logging
from datetime import timedelta

from django.core.management import call_command
from django.db.models import F
from django.utils import timezone

from .models import Job

log = logging.getLogger(__name__)

# Only these management commands can be queued.
ALLOWED = {"ingest_docs", "index_products", "sync_store", "import_products"}
RETRY_DELAY = 60            # seconds, multiplied by the attempt number


def enqueue(command, *args, max_attempts=3):
    if command not in ALLOWED:
        raise ValueError(f"'{command}' cannot be queued. Allowed: {sorted(ALLOWED)}")
    job = Job.objects.create(command=command, args=[str(a) for a in args], max_attempts=max_attempts)
    log.info("queued job %s: %s %s", job.id, command, job.args)
    return job


def requeue_stale(minutes=60):
    """A worker that crashed leaves its job 'running' forever; put those back in the queue.
    index_products is safe to rerun because finished batches are kept."""
    cutoff = timezone.now() - timedelta(minutes=minutes)
    n = Job.objects.filter(status=Job.RUNNING, started_at__lt=cutoff).update(status=Job.QUEUED)
    if n:
        log.warning("requeued %s stale job(s)", n)
    return n


def run_next():
    """Run the oldest job that is due. Returns the job, or None when nothing is waiting."""
    due = Job.objects.filter(status=Job.QUEUED, available_at__lte=timezone.now()).order_by("id")[:10]
    for job in due:
        # claim it with one UPDATE: if two workers race, only one gets claimed == 1
        claimed = Job.objects.filter(id=job.id, status=Job.QUEUED).update(
            status=Job.RUNNING, started_at=timezone.now(), attempts=F("attempts") + 1)
        if claimed:
            job.refresh_from_db()
            _execute(job)
            return job
    return None


def _execute(job):
    out = io.StringIO()
    try:
        call_command(job.command, *job.args, stdout=out, stderr=out)
    except (Exception, SystemExit) as exc:
        log.exception("job %s (%s) failed on attempt %s", job.id, job.command, job.attempts)
        job.error = f"{type(exc).__name__}: {exc}"[:2000]
        if job.attempts < job.max_attempts:
            job.status = Job.QUEUED
            job.available_at = timezone.now() + timedelta(seconds=RETRY_DELAY * job.attempts)
        else:
            job.status = Job.FAILED
            job.finished_at = timezone.now()
    else:
        job.status, job.error, job.finished_at = Job.DONE, "", timezone.now()
        log.info("job %s (%s) done", job.id, job.command)
    job.output = out.getvalue()[-5000:]
    job.save()
