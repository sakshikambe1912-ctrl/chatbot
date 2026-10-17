from django.db import models
from pgvector.django import VectorField, HnswIndex
from catalog.models import Store, Product
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.utils import timezone

class Document(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    doc_type = models.CharField(max_length=20, default="policy")  # policy, faq, review, spec
    source = models.CharField(max_length=300, blank=True)
    content_hash = models.CharField(max_length=64)


class Chunk(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    document = models.ForeignKey(Document, null=True, blank=True,
                                 on_delete=models.CASCADE, related_name="chunks")
    product = models.OneToOneField(Product, null=True, blank=True, on_delete=models.CASCADE)
    text = models.TextField()
    embedding = VectorField(dimensions=768)   # nomic-embed-text = 768
    meta = models.JSONField(default=dict)
 
    class Meta:
        indexes = [GinIndex(SearchVector("text", config="english"), name="chunk_text_fts")]

class Job(models.Model):
    """A background task: a management command to run later (see ingestion/jobs.py)."""
    QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

    command = models.CharField(max_length=60)
    args = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, default="queued", db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    available_at = models.DateTimeField(default=timezone.now)
    error = models.TextField(blank=True)
    output = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"#{self.pk} {self.command} {self.status}"