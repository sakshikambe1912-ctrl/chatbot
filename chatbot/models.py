from django.db import models
from catalog.models import Store

class Conversation(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    session_id = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=255, blank=True)
    preferences = models.JSONField(default=dict, blank=True)
    last_shown_ids = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE,
                                     related_name="messages")
    role = models.CharField(max_length=10)
    content = models.TextField()
    cards = models.JSONField(default=list, blank=True)
    feedback = models.SmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)