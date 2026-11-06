"""Parser exceptions carrying a stable issue code for the validation layer."""

from __future__ import annotations


class DocumentParseError(Exception):
    """Raised when a document cannot be opened/parsed. ``code`` maps to an Issue code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CorruptedDocumentError(DocumentParseError):
    def __init__(self, message: str) -> None:
        super().__init__("CORRUPTED_FILE", message)


class EncryptedDocumentError(DocumentParseError):
    def __init__(self, message: str) -> None:
        super().__init__("ENCRYPTED_FILE", message)
