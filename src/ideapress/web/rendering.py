"""ideapress.web.rendering — the one Jinja environment every page renders through.

MirrorWall supplies the shell, the component macros, the design tokens, autoescaping and
``StrictUndefined``; this module supplies only what is IdeaPress's — the product name, the
navigation and this application's own template directory.

Autoescaping is not optional here and is never bypassed. IdeaPress renders more model output than
anything else in the suite, and risk S1 names unescaped model output as its highest-impact security
risk: no template in this package applies ``| safe`` to anything a model produced.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mirrorwall import create_template_environment

from ideapress.__about__ import __version__

if TYPE_CHECKING:
    from jinja2 import Environment

__all__ = ["NAV_ITEMS", "render", "templates"]

_TEMPLATES_DIR = Path(__file__).parent / "templates"

NAV_ITEMS: tuple[dict[str, str], ...] = (
    {"key": "projects", "href": "/", "label": "Projects"},
    {"key": "backends", "href": "/backends", "label": "Backends"},
    {"key": "system", "href": "/system", "label": "System"},
)


@lru_cache(maxsize=1)
def templates() -> Environment:
    """Return the process-wide Jinja environment, building it on first use.

    Cached because templates are compiled and cached on the environment: a per-request environment
    recompiles the layout on every page view, which the 300 ms render budget (spec §15) does not
    have room for.
    """
    return create_template_environment(
        app_template_dirs=(_TEMPLATES_DIR,),
        globals_={
            "product_name": "IdeaPress",
            "product_version": __version__,
            "nav_items": NAV_ITEMS,
            "theme_storage_key": "ideapress-theme",
            # Telemetry is an optional extra (spec §5, §16): no bar unless sweatmeter is present,
            # and the application must be complete without it.
            "show_telemetry_bar": False,
        },
    )


def render(template_name: str, /, **context: Any) -> str:
    """Render ``template_name`` with ``context``.

    Args:
        template_name: Path relative to the template search path.
        **context: Template variables. Every value is escaped on output; pass model-produced text
            as a plain string and never as markup.

    Returns:
        The rendered HTML.
    """
    return templates().get_template(template_name).render(**context)
