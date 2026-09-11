"""ideapress.web.rendering — the one Jinja environment every page renders through.

MirrorWall supplies the shell, the component macros, the design tokens, autoescaping and
``StrictUndefined``; this module supplies only what is IdeaPress's — the product name, the
navigation and this application's own template directory.

Autoescaping is not optional here and is never bypassed. IdeaPress renders more model output than
anything else in the suite, and risk S1 names unescaped model output as its highest-impact security
risk: no template in this package applies ``| safe`` to anything a model produced.
"""

from __future__ import annotations

from functools import lru_cache, partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mirrorwall import create_template_environment
from mirrorwall.static import asset_url

from ideapress.__about__ import __version__

if TYPE_CHECKING:
    from jinja2 import Environment

__all__ = [
    "APP_STATIC_URL_PREFIX",
    "APP_TABS",
    "NAV_ITEMS",
    "configure_shell",
    "render",
    "templates",
]

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
APP_STATIC_URL_PREFIX = "/static/ideapress"
"""Where this application's own assets are served from; MirrorWall's stay under
`/static/mirrorwall`."""

APP_TABS: tuple[tuple[str, str], ...] = (
    ("freeweight", "FreeWeight"),
    ("loadcoach", "LoadCoach"),
    ("ideapress", "IdeaPress"),
    ("promptcadence", "PromptCadence"),
)
"""The suite's four applications, in the console's order, for the top-bar tab strip (row WM2).
Rendered only when ``[console] url`` is set; a peer is reached through the console
(``<url>/apps/<name>``), never on its own loopback port."""

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
    environment = create_template_environment(
        app_template_dirs=(_TEMPLATES_DIR,),
        globals_={
            "product_name": "IdeaPress",
            "product_version": __version__,
            "nav_items": NAV_ITEMS,
            "theme_storage_key": "ideapress-theme",
            # MirrorWall 0.3 opt-ins (row WM2, design brief §6): the product name as a link home
            # and the tab strip, which renders nothing until `configure_shell` hands it a console
            # URL. No telemetry bar here, so no meters.
            "product_href": "/",
            "console_url": "",
            "app_tabs": APP_TABS,
            "own_app": "ideapress",
        },
    )
    # This application's own assets (`workspace.css`, `workspace.js`, `diff.js`) live beside its
    # templates and are served from `/static/ideapress/` (web/app.py mounts it). They were rendered
    # through MirrorWall's `asset_url` until row WM2, which put them under `/static/mirrorwall/`,
    # where nothing served them — the workspace's live section never updated in a browser.
    environment.filters["app_asset_url"] = partial(
        asset_url, prefix=APP_STATIC_URL_PREFIX, root=_STATIC_DIR
    )
    return environment


def configure_shell(*, console_url: str) -> None:
    """Hand the shell what only the running configuration knows.

    Args:
        console_url: ``[console] url`` — WeightRoomGym's base URL, or ``""`` for no tab strip.
    """
    templates().globals["console_url"] = console_url.rstrip("/")


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
