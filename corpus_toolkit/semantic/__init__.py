"""Semantic (vector) search: the shared build and serve halves.

One implementation for every corpus. Before this it lived in a single repo's src/, so a
second corpus wanting semantic search had to copy it -- and the copies would diverge on
exactly the details that are expensive to rediscover (the convert-once optimisation, the
dim probe, the offline env flags).
"""
from .embedders import make_embedder                                   # noqa: F401
