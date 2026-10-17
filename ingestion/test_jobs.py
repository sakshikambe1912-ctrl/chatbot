from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from ingestion import jobs
from ingestion.models import Job


class JobQueueTests(TestCase):
    def test_unknown_command_is_refused(self):
        with self.assertRaises(ValueError):
            jobs.enqueue("flush")
        self.assertEqual(Job.objects.count(), 0)

    def test_job_runs_and_is_marked_done(self):
        job = jobs.enqueue("ingest_docs", "--store", "4")
        with mock.patch.object(jobs, "call_command") as run:
            done = jobs.run_next()
        self.assertEqual(done.id, job.id)
        run.assert_called_once()
        self.assertEqual(run.call_args.args, ("ingest_docs", "--store", "4"))
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (Job.DONE, 1))
        self.assertIsNotNone(job.finished_at)

    def test_nothing_queued_returns_none(self):
        self.assertIsNone(jobs.run_next())

    def test_running_job_is_not_picked_up_again(self):
        Job.objects.create(command="index_products", status=Job.RUNNING, started_at=timezone.now())
        self.assertIsNone(jobs.run_next())

    def test_failure_is_retried_later_then_marked_failed(self):
        job = jobs.enqueue("index_products", max_attempts=2)
        with mock.patch.object(jobs, "call_command", side_effect=CommandError("boom")):
            jobs.run_next()
            job.refresh_from_db()
            self.assertEqual((job.status, job.attempts), (Job.QUEUED, 1))
            self.assertIn("boom", job.error)
            self.assertIsNone(jobs.run_next())                      # waiting for its retry time
            Job.objects.update(available_at=timezone.now())
            jobs.run_next()
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (Job.FAILED, 2))
        self.assertIsNotNone(job.finished_at)

    def test_job_that_exits_is_a_failure_not_a_crash(self):
        job = jobs.enqueue("ingest_docs", max_attempts=1)
        with mock.patch.object(jobs, "call_command", side_effect=SystemExit(1)):
            jobs.run_next()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.FAILED)

    def test_stale_running_job_goes_back_to_the_queue(self):
        old = Job.objects.create(command="index_products", status=Job.RUNNING,
                                 started_at=timezone.now() - timedelta(hours=3))
        fresh = Job.objects.create(command="index_products", status=Job.RUNNING, started_at=timezone.now())
        self.assertEqual(jobs.requeue_stale(minutes=60), 1)
        old.refresh_from_db(); fresh.refresh_from_db()
        self.assertEqual((old.status, fresh.status), (Job.QUEUED, Job.RUNNING))


class EnqueueCommandTests(TestCase):
    def test_arguments_are_passed_through(self):
        call_command("enqueue_job", "ingest_docs", "--store", "4")
        job = Job.objects.get()
        self.assertEqual((job.command, job.args, job.status), ("ingest_docs", ["--store", "4"], Job.QUEUED))

    def test_refuses_commands_that_are_not_allowed(self):
        with self.assertRaises(CommandError):
            call_command("enqueue_job", "flush")
        self.assertEqual(Job.objects.count(), 0)

    def test_worker_once_runs_the_queue_and_exits(self):
        jobs.enqueue("index_products")
        with mock.patch.object(jobs, "call_command"):
            call_command("run_jobs", "--once")
        self.assertEqual(Job.objects.get().status, Job.DONE)
