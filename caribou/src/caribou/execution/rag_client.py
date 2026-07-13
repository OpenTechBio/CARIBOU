"""
RAG (Retrieval Augmented Generation) client initialization and management.

This module handles:
- Lazy initialization of the RAG singleton
- RAG client access for agents
"""
from __future__ import annotations

import threading

from rich.console import Console

from caribou.rag.RetrievalAugmentedGeneration import RetrievalAugmentedGeneration


# --- Lazily initialize RAG ---
_RAG_SINGLETON = None
# Guards the check-then-set of _RAG_SINGLETON. Server sessions run in
# background threads, so concurrent first calls could otherwise construct
# two RetrievalAugmentedGeneration instances (each of which loads a model).
_RAG_LOCK = threading.Lock()


def get_rag_client(console: Console) -> RetrievalAugmentedGeneration:
    """Get or initialize the RAG client singleton."""
    global _RAG_SINGLETON
    if _RAG_SINGLETON is not None:
        return _RAG_SINGLETON
    with _RAG_LOCK:
        if _RAG_SINGLETON is None:
            console.print("[cyan]Initializing RAG model (this may take a moment)...[/cyan]")
            _RAG_SINGLETON = RetrievalAugmentedGeneration()
    return _RAG_SINGLETON
