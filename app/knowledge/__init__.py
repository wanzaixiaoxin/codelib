"""代码知识工程层（胶水层）。"""
from .schema import KnowledgeStore
from . import build, query

__all__ = ["KnowledgeStore", "build", "query"]
