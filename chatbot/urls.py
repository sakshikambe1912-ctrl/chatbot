from django.urls import path
from . import views

urlpatterns = [
    path("chat/", views.chat),
    path("live-search/", views.live_search),
    path("history/", views.history),
    path("conversations/", views.conversations),
    path("conversations/new/", views.conversation_new),
    path("conversations/reset/", views.conversation_reset),
]