# IdeaPress — content types

A content type supplies the unit taxonomy, the validators specific to its structure, its default
workflow and its export templates. **The engine knows only units and requirements** — never
chapters, sections or quests. That is what lets a new content type be added without touching the
workflow.

## What ships at 1.0

| Type | For | Unit shape |
|---|---|---|
| `article` | A single piece of prose with an argument | Sections |
| `report` | Structured findings with a summary | Sections with a required summary |

More content types are a post-1.0 extension (spec §21). The registry is open, and this page is what
you need to add one.

## The protocol

A content type is any object satisfying `ContentType`, discovered from the
`ideapress.content_types` entry-point group:

```python
from ideapress.content_types.registry import ContentType

class Novel:
    """A long-form narrative, in chapters."""

    id = "novel"
    version = "1.0"
    unit_noun = "chapter"
    default_workflow = "narrative"

    def validators(self):
        """Validators specific to this structure, added to the deterministic set."""
        return (ChapterOpeningValidator(),)

    def export_templates(self):
        """Templates keyed by format id."""
        return {"markdown": "novel/markdown.md.j2"}
```

```toml
# pyproject.toml
[project.entry-points."ideapress.content_types"]
novel = "my_package.novel:Novel"
```

`ideapress project create --content-type novel` then uses it, and the project records the content
type **and its version**, so a later change to the type never rewrites units committed under the
old one.

## The rules a content type must respect

* **It adds validators; it does not remove them.** The deterministic set every unit passes is the
  engine's, and a content type cannot opt out of it. A type that could would be a way to commit
  content that failed validation.
* **It does not decide when work is finished.** Gates belong to the engine.
* **Its templates escape everything.** Unit content is model output; a template that marked it safe
  would be the one gap the sanitization sweep exists to prevent.
* **Its version is part of provenance.** Bump it when the unit taxonomy or the validators change,
  because a project records what it was created with.

## Validators

A validator returns outcomes; it never rewrites content. Blocking outcomes stop a commit, advisory
ones inform the critique. Be careful making one blocking: a validator that is too strict blocks
legitimate content, which is a worse failure than one that is too lenient — the audit and the
critique are behind it.
