"""Compose resource-specific RAG routes without changing their public URLs."""

from .common import router
from . import documents, graphs, jobs, knowledge_bases, operations  # noqa: F401

__all__ = ["router"]
