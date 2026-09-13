# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""Reusable search and index mechanisms for anything that holds a collection.

This lives in the foundation rather than in the platform because both sides need
it and neither owns it. The platform indexes pins, tasks and memories; host-side
tools index their own collections and run in a venv that deliberately does not
carry the platform SDK. One implementation, two consumers, no copy.

- `vector_store` : the `VectorStore` protocol and the dependency-free
                   `BruteForceVectorStore`.
- `sqlite`       : the SQLite and vector hybrid index (`HybridIndex`).

faiss backends stay platform-side in `kdcube_ai_app.infra.index.faiss`, because
they carry faiss and numpy and a foundation package should not. An index *uses*
a backend; it does not contain one.
"""
from .vector_store import VectorStore, BruteForceVectorStore

__all__ = ["VectorStore", "BruteForceVectorStore"]
