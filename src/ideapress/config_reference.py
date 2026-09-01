"""ideapress.config_reference — the configuration reference, generated from :class:`Settings`.

The plan names documentation drift from the generated configuration reference as a likely failure
mode of this phase, and the answer is that the reference is not written by hand at all. Every
section, key, type, default and description here is read out of the pydantic models, so a setting
added without documentation is impossible: the setting *is* the documentation, and a test asserts
the committed `docs/configuration.md` still equals what this produces.

Run it directly to regenerate:

```bash
python -m ideapress.config_reference > docs/configuration.md
```
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import BaseModel

from ideapress.config import ENV_PREFIX, Settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pydantic.fields import FieldInfo

__all__ = ["render_reference", "sections"]

_HEADER = """# IdeaPress — configuration reference

**Generated from `ideapress.config.Settings`. Do not edit by hand.**

```bash
python -m ideapress.config_reference > docs/configuration.md
```

A test asserts this file equals what the generator produces, so a setting cannot be added, renamed
or re-defaulted without the reference following it in the same commit.

## Where configuration comes from

Precedence, lowest to highest:

1. built-in defaults — **everything has one**, and `ideapress serve` needs no configuration file at
   all (spec §20 AC1);
2. `config.toml` — `ideapress config path` prints where it is looked for, `ideapress config init`
   writes a commented example;
3. `IDEAPRESS_`-prefixed environment variables, nested with `__`
   (`IDEAPRESS_INFERENCE__MODE=loadcoach`);
4. explicit command-line overrides.

Merging is **per leaf field**, not per section: setting one key of `[server]` never discards its
siblings. `ideapress config show` reports which layer produced every value, and
`ideapress config validate` refuses an invalid file with the offending key named.

"""

_FOOTER = """
## Refusals

Some values are refused at start-up rather than accepted and worked around, because silently
honouring one would produce a system the operator believes is configured differently from how it
behaves. Each refusal names the key.

| Configuration | Why it is refused |
|---|---|
| Non-loopback `server.host`, no `server.allowed_hosts` | Reachable from any page the user visits |
| `server.host = "0.0.0.0"` without `allow_lan_exposure` | The same exposure, spelled differently |
| `execution.max_concurrent_stages` above 1 | Two models on one GPU; IdeaPress has no queue |
| `inference.fallback_mode` naming no real mode | It reads as configured resilience and is none |
| `inference.fallback_mode` equal to `inference.mode` | A backend cannot fall back to itself |
| A `[models.stages]` key that is not a stage | It looks like a binding and binds nothing |
| A model-using stage with no `[models.stages]` binding | The stage would fail when it ran |
| `loadcoach.job_stages` naming a non-model stage | It would queue nothing and say nothing |

The first two are `INSECURE_BINDING` (ADR-0026); the third is ADR-0038. The rest are
`CONFIGURATION_ERROR`, raised before anything opens a socket.
"""


def _type_name(annotation: Any) -> str:
    """A readable type for the reference: ``str``, ``int``, ``list[str]``, ``one of a | b``."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is None:
        return getattr(annotation, "__name__", str(annotation).replace("typing.", ""))
    if args and all(isinstance(arg, str) for arg in args):
        return "one of " + " | ".join(f"`{arg}`" for arg in args)
    name = getattr(origin, "__name__", str(origin))
    if name in {"UnionType", "Union"}:
        return " | ".join(_type_name(arg) for arg in args if arg is not type(None))
    if args:
        rendered = ", ".join("..." if arg is Ellipsis else _type_name(arg) for arg in args)
        return f"{name}[{rendered}]"
    return name


def _default(field: FieldInfo) -> str:
    """The default, rendered the way it would be written in `config.toml`."""
    value = field.default
    if value is None or repr(value) == "PydanticUndefined":
        factory = field.default_factory
        if factory is not None:
            return "*(section)*"
        return "*(none)*"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, str):
        return f'`"{value}"`' if value else "*(empty)*"
    if isinstance(value, tuple):
        return f"`{list(value)}`" if value else "`[]`"
    return f"`{value}`"


def sections(
    model: type[BaseModel] = Settings, prefix: str = ""
) -> Iterator[tuple[str, type[BaseModel]]]:
    """Every configuration section, depth first, as ``(toml_path, model)``.

    Args:
        model: The model to walk. Defaults to the whole of :class:`Settings`.
        prefix: The TOML path so far, for recursion.

    Yields:
        One entry per nested settings model, in declaration order — which is the order a person
        reads a configuration file in, and therefore the order the reference should be in.
    """
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            path = f"{prefix}{name}"
            yield (path, annotation)
            yield from sections(annotation, prefix=f"{path}.")


def _rows(model: type[BaseModel], path: str) -> Iterator[str]:
    """One table row per scalar field of a section."""
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            continue
        env = f"`{ENV_PREFIX}{path.replace('.', '__').upper()}__{name.upper()}`"
        description = (field.description or "").replace("\n", " ").strip()
        yield f"| `{name}` | {_type_name(annotation)} | {_default(field)} | {env} | {description} |"


def render_reference() -> str:
    """Render the whole reference.

    Returns:
        Markdown, deterministic for a given :class:`Settings` — no wall-clock stamp, no version
        number and no unsorted iteration, so a regeneration that changes nothing produces a file
        that differs in nothing and the drift test says something real.
    """
    lines = [_HEADER.rstrip("\n"), ""]
    for path, model in sections():
        rows = list(_rows(model, path))
        if not rows:
            continue
        lines.append(f"## `[{path}]`")
        lines.append("")
        doc = (model.__doc__ or "").strip().split("\n\n")[0].replace("\n    ", " ").strip()
        if doc:
            lines.append(doc)
            lines.append("")
        lines.append("| Key | Type | Default | Environment variable | Notes |")
        lines.append("| --- | --- | --- | --- | --- |")
        lines.extend(rows)
        lines.append("")
    lines.append(_FOOTER.strip("\n"))
    return "\n".join(lines).rstrip("\n") + "\n"


if __name__ == "__main__":  # pragma: no cover — the regeneration entry point
    import sys

    sys.stdout.write(render_reference())
