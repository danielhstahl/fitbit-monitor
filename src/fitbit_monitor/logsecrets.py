"""Keep credentials out of the log, whatever a call site happens to interpolate.

``logsafe`` keeps *measurements* out of the log; this module keeps the keys out.
Both are filters on the handler rather than rules for the caller, because the leak
that matters is the one nobody wrote on purpose: a debug line that quotes a request
header, a plugin that logs its config, an ``HTTPError`` whose message carries the
``?access_token=`` of a URL, a traceback that reprs a ``TokenSet``.

Two mechanisms, both applied to every record before it reaches a handler:

**Pattern pass** - a key that names a secret (``access_token``, ``client_secret``,
``password``, ...) never keeps its value, in ``key=value``, ``key: value``,
``"key": "value"`` or Python-dict shape; and an ``Authorization:`` header keeps its
scheme and loses its credential.

**Remembered pass** - the values themselves. :func:`remember` records a string that
this process knows is a secret, and from then on it is replaced wherever it appears,
however it was formatted. ``TokenSet`` registers its own access and refresh tokens and
:class:`~fitbit_monitor.config.ApiSettings` registers its client secret, so anything
read from the token store is masked from the moment it is loaded - no call site has to
cooperate, and no call site can forget.

The mask is ``[REDACTED]``: loud, fixed-width-ish, and never mistakable for a real
value. Redaction is best effort for prose that paraphrases a secret ("the token ends
in 4f2a") and exact for the values this process holds.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Iterable

#: Placeholder for a secret. Required to be this exact string: a user greps for it and
#: a test asserts on it.
MASK = "[REDACTED]"

#: Key names whose value is a credential. Matched at word boundaries, so
#: ``client_secret_set`` (a bool, printed by ``fm config show``) is not masked while
#: ``client_secret`` is. ``authorization`` is handled by its own pattern below, which
#: gets to keep the scheme because "Bearer [REDACTED]" says something useful.
SECRET_KEYS: tuple[str, ...] = (
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "session_token",
    "client_secret",
    "client-secret",
    "clientsecret",
    "app_secret",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "credentials",
)

#: Characters that end a bare (unquoted) value. Deliberately includes ``&`` so a token
#: in a query string stops at the next parameter, and ``)``/``]``/``}`` so a value
#: inside a repr or a parenthetical does not swallow the structure. Four characters is
#: the floor for a *keyed* value: it is what keeps the sentence "token: the token was
#: renewed" readable while still matching every credential shape that occurs.
_BARE_VALUE = r"[^\s,;&\"'()\[\]{}]+"

_KEYS_PATTERN = "|".join(re.escape(key) for key in SECRET_KEYS)

#: ``key = value`` in any of the shapes a log line takes: TOML/argv (``a=b``), prose or
#: YAML (``a: b``), JSON (``"key": "b"``), a Python dict repr (``{'a': 'b'}``). The key
#: is bounded on alphanumerics only, so ``client_secret_set`` - a bool this project
#: prints on purpose - is not a secret while ``client_secret`` is.
_KEYED_RE = re.compile(
    r"(?P<key>(?<![0-9A-Za-z])(?:" + _KEYS_PATTERN + r")(?![0-9A-Za-z]))(?P<keyq>[\"']?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?:(?P<q>[\"'])(?P<quoted>[^\r\n]*?)(?P=q)|(?P<value>[^\s,;&\"'()\[\]{}]{4,}))",
    re.IGNORECASE,
)

#: An ``Authorization`` header keeps its scheme and loses its credential: showing that
#: a request was bearer-authenticated is diagnostics; showing the bearer is not.
_AUTH_RE = re.compile(
    r"(?P<key>\b(?:proxy-authorization|authorization|x-api-key|x-auth-token)\b)(?P<keyq>[\"']?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<q>[\"']?)"
    # `?+` (atomic, Python 3.11+): once a scheme word is seen, the credential has to
    # follow it. Without it the engine backtracks to "no scheme", calls ``Bearer`` the
    # credential, and a line that was already masked comes out doubly masked.
    r"(?P<scheme>bearer\s+|basic\s+|token\s+|digest\s+)?+"
    r"(?P<value>" + _BARE_VALUE + r")",
    re.IGNORECASE,
)

#: Values shorter than this are not worth remembering: a four-character "secret" would
#: mask that substring everywhere and turn the log into noise.
MIN_REMEMBERED_LENGTH = 8

#: The registry is bounded and oldest-out, because a long-lived process refreshes tokens
#: forever and a forgotten value has no reason to stay.
MAX_REMEMBERED = 64

_lock = threading.Lock()
_remembered: dict[str, None] = {}


def remember(value: str | None) -> bool:
    """Record that ``value`` is a secret, so it is masked wherever it turns up.

    Returns True when the value was new to the registry. Empty, short and
    already-known values are ignored (the second one keeps insertion order stable).
    """
    if not value or len(value) < MIN_REMEMBERED_LENGTH:
        return False
    with _lock:
        if value in _remembered:
            return False
        _remembered[value] = None
        while len(_remembered) > MAX_REMEMBERED:
            _remembered.pop(next(iter(_remembered)))
        return True


def remember_all(values: Iterable[str | None]) -> int:
    """Remember several values; returns how many were new."""
    return sum(1 for value in values if remember(value))


def forget(value: str) -> bool:
    """Stop masking one specific value (used by tests and by ``fm auth logout``)."""
    with _lock:
        if value not in _remembered:
            return False
        del _remembered[value]
        return True


def clear() -> int:
    """Forget every remembered value; returns how many were dropped."""
    with _lock:
        count = len(_remembered)
        _remembered.clear()
        return count


def remembered() -> tuple[str, ...]:
    """The registered values, longest first (the order the masking runs in)."""
    with _lock:
        return tuple(sorted(_remembered, key=len, reverse=True))


def _mask_remembered(text: str) -> str:
    for value in remembered():
        if value in text:
            text = text.replace(value, MASK)
    return text


def _sub_keyed(match: re.Match[str]) -> str:
    whole = match.group(0)
    if MASK in whole:  # already masked by the value pass - never mask a mask
        return whole
    key, keyq = match.group("key"), match.group("keyq") or ""
    sep, quote = match.group("sep"), match.group("q")
    if quote:
        return f"{key}{keyq}{sep}{quote}{MASK}{quote}"
    return f"{key}{keyq}{sep}{MASK}"


def _sub_auth(match: re.Match[str]) -> str:
    whole = match.group(0)
    if MASK in whole:
        return whole
    scheme = match.group("scheme") or ""
    return (
        f"{match.group('key')}{match.group('keyq') or ''}{match.group('sep')}"
        f"{match.group('q')}{scheme}{MASK}"
    )


def redact_secrets(text: str) -> str:
    """Mask every secret this module can recognise in ``text``.

    Everything else comes back byte-for-byte unchanged, so a log line a user depends on
    still reads the same: redaction that destroys diagnostics gets turned off, and an
    off filter protects nothing. Running it twice changes nothing - a value the
    remembered pass already masked is not masked again by the shape patterns - which is
    what lets a record pass through more than one installed filter.
    """
    if not text:
        return text
    # Remembered values first: they are exact and know no syntax, so a token inside a
    # URL, a dict or a traceback is masked before the shape-matching patterns run.
    text = _mask_remembered(text)
    text = _AUTH_RE.sub(_sub_auth, text)
    return _KEYED_RE.sub(_sub_keyed, text)


def scrub_exception(record: logging.LogRecord) -> None:
    """Replace a record's traceback with a redacted one, in place.

    Tracebacks are where secrets hide most happily - the frame reprs an object that
    holds a token - and ``Formatter`` would render them straight from ``exc_info``.
    Pre-computing ``exc_text`` is how a filter gets the last word: ``Formatter`` uses
    it verbatim instead of re-formatting the exception.
    """
    if record.exc_text:
        record.exc_text = redact_secrets(record.exc_text)
        return
    if record.exc_info and record.exc_info[1] is not None:
        record.exc_text = redact_secrets(logging.Formatter().formatException(record.exc_info))


class SecretRedactingFilter(logging.Filter):
    """Mask credentials in every record that passes through, and say nothing else.

    The record is flattened (``msg % args``) on the way past, exactly like
    :class:`~fitbit_monitor.logsafe.RedactingFilter`: a token that arrives as a
    deferred ``%s`` argument is as readable in the log as one already interpolated, and
    it is the one case a reviewer cannot see by reading the call site.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken % call is not this filter's problem
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        scrub_exception(record)
        return True


#: Name the CLI gives its filter, so repeated configuration does not stack copies.
FILTER_NAME = "fitbit_monitor_secrets"


def install(handler: logging.Handler | None = None, *, logger: logging.Logger | None = None) -> SecretRedactingFilter:
    """Attach the filter to ``handler`` (default: every handler on ``logger`` or the root)."""
    added = SecretRedactingFilter()
    added.name = FILTER_NAME
    targets: list[logging.Handler] = (
        [handler] if handler is not None else list((logger or logging.getLogger()).handlers)
    )
    for target in targets:
        if not any(getattr(existing, "name", None) == FILTER_NAME for existing in target.filters):
            target.addFilter(added)
    return added


__all__ = [
    "FILTER_NAME",
    "MASK",
    "MAX_REMEMBERED",
    "MIN_REMEMBERED_LENGTH",
    "SECRET_KEYS",
    "SecretRedactingFilter",
    "clear",
    "forget",
    "install",
    "redact_secrets",
    "remember",
    "remember_all",
    "remembered",
    "scrub_exception",
]
