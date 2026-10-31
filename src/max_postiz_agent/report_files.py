"""Короткий повтор только атомарного коммита уже fsync-нутого локального отчёта."""

import os
import time


_WINDOWS = os.name == "nt"
_replace = os.replace
_monotonic = time.monotonic
_sleep = time.sleep
_RETRY_WINERRORS = {5, 32, 33}
_MAX_SECONDS = 1.0
_INTERVAL_SECONDS = 0.05
_MAX_ATTEMPTS = 21


def replace_report_file(temporary, destination):
    """Не перезаписываем temp, не меняем ACL и не повторяем получение отчёта."""
    deadline = _monotonic() + _MAX_SECONDS
    last_error = None
    for _ in range(_MAX_ATTEMPTS):
        if last_error is not None:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise last_error
            _sleep(min(_INTERVAL_SECONDS, remaining))
            # Проснувшись поздно, не начинаем новую мутацию после deadline.
            if _monotonic() >= deadline:
                raise last_error
        try:
            _replace(temporary, destination)
            return
        except OSError as error:
            winerror = getattr(error, "winerror", None)
            if not _WINDOWS or type(winerror) is not int or winerror not in _RETRY_WINERRORS:
                raise
            last_error = error
    raise last_error
