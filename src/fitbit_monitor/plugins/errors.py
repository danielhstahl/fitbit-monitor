"""Plugin errors. Everything here is *expected*: the host catches, reports and moves on.

The split matters because the host treats these differently. A load error means a
plugin is unusable and is reported by ``fm plugins list``; a runtime error means a
plugin broke mid-run and fails the run with a nonzero exit; a timeout is a runtime
error with its own name so a runaway plugin is not mistaken for a broken one.
"""

from __future__ import annotations

from ..errors import FmError

#: Stages a plugin can fail at, named the same way in the report and in the CLI.
STAGES = ("discover", "import", "register", "configure", "run", "timeout")


class PluginError(FmError):
    """Base class for plugin failures. Carries the plugin name and the stage."""

    exit_code = 1
    stage = "discover"

    def __init__(self, message: str, *, plugin: str | None = None, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.plugin = plugin

    def __str__(self) -> str:
        prefix = f"{self.plugin or '<plugin>'}: " if self.plugin else ""
        text = f"{prefix}{self.message}"
        return f"{text}\n  hint: {self.hint}" if self.hint else text


class PluginRegistrationError(PluginError):
    """The object is not a usable plugin: missing members, bad name, unknown API."""

    stage = "register"


class PluginImportError(PluginError):
    """The module raised while being imported."""

    stage = "import"


class PluginConfigError(PluginError):
    """The plugin rejected its own ``[plugins.<name>]`` section."""

    stage = "configure"


class PluginRuntimeError(PluginError):
    """The plugin's ``run()`` raised. Other plugins still get their turn."""

    stage = "run"

    def __init__(self, message, *, plugin=None, hint=None, cause: BaseException | None = None) -> None:
        super().__init__(message, plugin=plugin, hint=hint)
        self.cause = cause


class PluginTimeout(PluginRuntimeError):
    """The plugin exceeded its wall-clock budget and was abandoned."""

    stage = "timeout"
