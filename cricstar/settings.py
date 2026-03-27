# ruff: noqa: F401
import functools
import warnings

# Python 3.12 compatibility
if not hasattr(warnings, "deprecated"):
    def _deprecated_compat(msg, *, category=DeprecationWarning, stacklevel=1):
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                warnings.warn(msg, category=category, stacklevel=stacklevel + 1)
                return func(*args, **kwargs)
            return wrapper
        return decorator
    warnings.deprecated = _deprecated_compat  # type: ignore

from settings.models import settings  # noqa: E402

warnings.warn(
    'Importing settings from this location is deprecated, use "from settings.models import settings" instead',
    DeprecationWarning,
    stacklevel=2,
)
