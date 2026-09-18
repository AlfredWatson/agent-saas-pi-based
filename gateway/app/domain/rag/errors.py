"""Transport-neutral errors emitted by RAG application code."""


class RagError(Exception):
    """A stable RAG machine code, independent of HTTP transport."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
