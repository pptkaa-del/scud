"""
Общие HTTP утилиты для всех сервисов.
"""

from .errors import UpstreamUnavailableError
from .problem import STATUS_TO_PROBLEM, ProblemDetails, ProblemTitle, ProblemType
from .retry_client import RetryHTTPClient

__all__ = [
    "ProblemDetails",
    "ProblemType",
    "ProblemTitle",
    "STATUS_TO_PROBLEM",
    "RetryHTTPClient",
    "UpstreamUnavailableError",
]
