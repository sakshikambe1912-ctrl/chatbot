import hashlib

import ollama
from django.db import transaction

from .models import Chunk, Document

EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768  # must match Chunk.embedding


def chunk_text(text, size=220, overlap=40):
    """Cut text into pieces of about `size` words; neighbours share `overlap` words."""
    words = text.split()
    if not words:
        return []
    out, i = [], 0
    while True:
        out.append(" ".join(words[i:i + size]))
        if i + size >= len(words):      # last piece reached the end of the text
            break
        i += size - overlap
    return out


def embed_batch(texts, prefix):
    """Turn texts into vectors with Ollama (32 at a time)."""
    vectors = []
    for i in range(0, len(texts), 32):
        batch = [prefix + t for t in texts[i:i + 32]]
        vectors += ollama.embed(model=EMBED_MODEL, input=batch)["embeddings"]
    for v in vectors:
        if len(v) != EMBED_DIM:
            raise ValueError(f"{EMBED_MODEL} returned {len(v)} numbers, expected {EMBED_DIM}")
    return vectors


def ingest_document(store, title, text, doc_type="policy", source="", force=False):
    """Store one document as chunks. Returns the number of chunks written,
    or 0 if the document had not changed."""
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    doc, _ = Document.objects.get_or_create(
        store=store, title=title,
        defaults={"doc_type": doc_type, "source": source, "content_hash": ""})
    if doc.content_hash == h and not force and doc.chunks.exists():
        return 0                                      # unchanged, skip

    pieces = chunk_text(text)
    # Embed BEFORE touching the database, so a failure leaves the old chunks intact.
    vectors = embed_batch([f"{title}. {p}" for p in pieces], "search_document: ")

    with transaction.atomic():
        doc.chunks.all().delete()
        Chunk.objects.bulk_create([
            Chunk(store=store, document=doc, text=p, embedding=v, meta={"title": title})
            for p, v in zip(pieces, vectors)
        ])
        doc.doc_type, doc.source, doc.content_hash = doc_type, source, h
        doc.save()
    return len(pieces)