"""Shared Jinja2 environment for rendering packaged templates.

Templates live under ``docker_disk_toolkit/templates`` and are loaded via
:class:`jinja2.PackageLoader`. XML templates are autoescaped; text templates
(Markdown, systemd units, crontab) are not.
"""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, select_autoescape

_ENV: Environment | None = None


def get_environment() -> Environment:
    """Return the shared, lazily-built Jinja2 environment."""
    global _ENV
    if _ENV is None:
        _ENV = Environment(
            loader=PackageLoader("docker_disk_toolkit", "templates"),
            autoescape=select_autoescape(["xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
    return _ENV


def render_template(name: str, /, **context: object) -> str:
    """Render the named packaged template with ``context``."""
    return get_environment().get_template(name).render(**context)
