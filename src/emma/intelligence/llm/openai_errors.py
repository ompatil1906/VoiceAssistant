 
# Copyright (c) 2026, QuantumLoopAI
# Modified for EMMA Receptionist by QuantumLoopAI.

"""Internal error classification for OpenAI service integrations."""

from openai import BadRequestError

from emma.support.errors import ErrorCategory


def classify_openai_error(exception: BaseException) -> ErrorCategory | None:
    """Classify an OpenAI SDK exception whose status code alone would mislead.

    A content filter (Azure OpenAI's, or an OpenAI-compatible moderation
    proxy's) rejects a request with an HTTP 400 whose ``code`` is
    ``content_filter``, which is ``ErrorCategory.CONTENT_FILTERED`` rather than
    the permanent ``INVALID_REQUEST`` the status code maps to.

    Args:
        exception: The exception to classify.

    Returns:
        The category, or None to fall back to the status code.
    """
    if isinstance(exception, BadRequestError) and exception.code == "content_filter":
        return ErrorCategory.CONTENT_FILTERED
    return None