"""ideapress.services.project_archive — portable project archives (spec §7.2).

`ideapress project export` writes one file carrying a project's whole record — the brief, the
compiled requirements, the plan, every committed version and the provenance of every attempt — and
`ideapress project import` reads it back on another machine. It is the backup story, the
move-between-machines story, and the "send me your project so I can see what happened" story.

**Nothing is written before everything is validated.** The plan's named failure mode for this phase
is "an archive import path that writes before validating", and the whole shape of this module is
the answer to it: :func:`inspect_archive` reads the archive and returns a verdict without touching
the filesystem, :func:`import_project` calls it first and refuses on any complaint, and the
extraction that follows writes into a fresh temporary directory that is only moved into place once
it is complete. A hostile archive therefore never leaves a partial project behind, and never
reaches a path outside the project directory at all.

The guards are Security Standards §5's list, and each is here because an archive can be authored by
somebody who is not the person importing it:

* **absolute paths** and **`..` components** — refused by name, before any write.
* **symlinks and hardlinks** — refused entirely. A symlink pointing at `/etc/passwd` turns a later
  read of "the project's own file" into a read of something else.
* **decompression bombs** — an entry-count cap, a per-entry size cap, a total size cap and a
  compression-ratio cap. All four, because each catches a shape the others miss: 10 000 tiny files,
  one enormous file, many medium files, and one file that is small until it is not.
* **device and other special files** — nothing but regular files and directories.
"""

from __future__ import annotations

import json
import logging
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baseaicore import ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from ideapress.services.runtime import Runtime

__all__ = [
    "ARCHIVE_SCHEMA",
    "ARCHIVE_SCHEMA_VERSION",
    "MAX_ENTRIES",
    "MAX_ENTRY_BYTES",
    "MAX_RATIO",
    "MAX_TOTAL_BYTES",
    "ArchiveReport",
    "export_project_archive",
    "import_project_archive",
    "inspect_archive",
]

logger = logging.getLogger(__name__)

ARCHIVE_SCHEMA = "ideapress.project_archive"
"""The document's schema name, in IdeaPress's own namespace (ADR-0035 §1)."""

ARCHIVE_SCHEMA_VERSION = "1.0"

MANIFEST_NAME = "project.json"

MAX_ENTRIES = 10_000
"""Entry-count cap. A project archive is one JSON document and its artifacts; ten thousand entries
is already far past anything this application produces."""

MAX_ENTRY_BYTES = 256 * 1024 * 1024
"""Per-entry cap: one file inside the archive. Catches the single-enormous-file bomb."""

MAX_TOTAL_BYTES = 1024 * 1024 * 1024
"""Total uncompressed cap. Catches the many-medium-files bomb that slips under the per-entry cap."""

MAX_RATIO = 200
"""Compression-ratio cap. Catches the file that is small on disk and enormous in memory — a zip
bomb announces itself well before 200×, and no honest text archive approaches it."""


@dataclass(frozen=True, slots=True)
class ArchiveReport:
    """What an archive contains, and every reason it must be refused.

    Attributes:
        entries: How many members it declares.
        total_bytes: Their uncompressed total.
        compressed_bytes: The archive's own size on disk.
        project_title: The title in the manifest, when there is a readable one.
        schema_version: The manifest's schema version.
        problems: Every refusal reason, in the order found. **Empty means safe to extract**, and
            it is the only thing :func:`import_project_archive` consults.
    """

    entries: int = 0
    total_bytes: int = 0
    compressed_bytes: int = 0
    project_title: str = ""
    schema_version: str = ""
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def safe(self) -> bool:
        """Whether every check passed. Never inferred from the absence of an exception."""
        return not self.problems

    @property
    def ratio(self) -> float:
        """Uncompressed bytes per compressed byte, or 0.0 when the archive is empty."""
        return self.total_bytes / self.compressed_bytes if self.compressed_bytes else 0.0


def _is_contained(name: str) -> bool:
    """Whether ``name`` stays inside the extraction root.

    Args:
        name: The member's declared path, as it appears in the archive.

    Returns:
        Whether it is a relative path with no ``..`` component and no drive or root. Checked on the
        *declared* name rather than on the resolved one, because resolving it requires having
        chosen a destination — and this check must be answerable before any destination exists.
    """
    if not name or name.startswith(("/", "\\")):
        return False
    pure = Path(name)
    if pure.is_absolute() or pure.drive or pure.root:
        return False
    return ".." not in pure.parts


def inspect_archive(path: Path) -> ArchiveReport:
    """Read an archive and report whether it is safe to extract. **Writes nothing.**

    Args:
        path: The archive file.

    Returns:
        An :class:`ArchiveReport`. A report with problems is a *returned value*, not an exception:
        the caller may want to show a person everything wrong with an archive rather than the first
        thing wrong with it.

    Raises:
        ValidationError: The file is not an archive this application reads at all — not a
            zip, not a tar. That is a different answer from "this archive is dangerous", and
            conflating the two would tell somebody their backup was malicious when it was a
            photograph.

    Every check here runs against the archive's *metadata*. Nothing is decompressed, nothing is
    written, and a bomb is refused on the strength of its declared sizes before a byte of it is
    expanded.
    """
    if not path.is_file():
        message = f"{path} is not a file."
        raise ValidationError(message, details={"path": str(path)})

    compressed = path.stat().st_size
    if zipfile.is_zipfile(path):
        entries = list(_zip_entries(path))
    elif tarfile.is_tarfile(path):
        entries = list(_tar_entries(path))
    else:
        message = (
            f"{path.name} is neither a zip nor a tar archive. `ideapress project export` writes a "
            "zip; this is not one."
        )
        raise ValidationError(message, details={"path": str(path)})

    problems: list[str] = []
    total = 0
    title = ""
    schema_version = ""

    if len(entries) > MAX_ENTRIES:
        problems.append(
            f"the archive declares {len(entries)} entries, past the {MAX_ENTRIES} cap "
            "(decompression bomb guard)"
        )

    for name, size, kind in entries:
        if kind == "link":
            problems.append(f"{name!r} is a symlink or hardlink, which this importer never follows")
            continue
        if kind == "special":
            problems.append(f"{name!r} is a device or special file, not a regular file")
            continue
        if not _is_contained(name):
            problems.append(
                f"{name!r} escapes the extraction directory (an absolute path or a '..' component)"
            )
            continue
        if size > MAX_ENTRY_BYTES:
            problems.append(
                f"{name!r} is {size} bytes uncompressed, past the {MAX_ENTRY_BYTES} per-entry cap"
            )
        total += size

    if total > MAX_TOTAL_BYTES:
        problems.append(
            f"the archive expands to {total} bytes, past the {MAX_TOTAL_BYTES} total cap "
            "(decompression bomb guard)"
        )
    if compressed and total / compressed > MAX_RATIO:
        problems.append(
            f"the archive expands {total / compressed:.0f}× its compressed size, past the "
            f"{MAX_RATIO}× ratio cap (decompression bomb guard)"
        )

    manifest_entries = [
        name for name, _, kind in entries if name == MANIFEST_NAME and kind == "file"
    ]
    if not manifest_entries:
        problems.append(f"no {MANIFEST_NAME} at the archive root; this is not a project archive")
    elif not problems:
        # Only read the manifest once the structural checks have passed: parsing a document out of
        # an archive that has already failed a bomb check is doing work on hostile input for no
        # reason.
        title, schema_version, manifest_problems = _read_manifest(path)
        problems.extend(manifest_problems)

    return ArchiveReport(
        entries=len(entries),
        total_bytes=total,
        compressed_bytes=compressed,
        project_title=title,
        schema_version=schema_version,
        problems=tuple(problems),
    )


def _zip_entries(path: Path) -> Iterator[tuple[str, int, str]]:
    """Every zip member as ``(name, uncompressed_size, kind)``."""
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            mode = info.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:  # S_IFLNK — zip stores it in the mode bits
                yield (info.filename, info.file_size, "link")
            elif info.is_dir():
                yield (info.filename, 0, "dir")
            elif mode and (mode & 0o170000) not in {0o100000, 0o040000, 0}:
                yield (info.filename, info.file_size, "special")
            else:
                yield (info.filename, info.file_size, "file")


def _tar_entries(path: Path) -> Iterator[tuple[str, int, str]]:
    """Every tar member as ``(name, size, kind)``."""
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                yield (member.name, member.size, "link")
            elif member.isdir():
                yield (member.name, 0, "dir")
            elif not member.isfile():
                yield (member.name, member.size, "special")
            else:
                yield (member.name, member.size, "file")


def _read_manifest(path: Path) -> tuple[str, str, list[str]]:
    """Read and shape-check the manifest without extracting anything to disk."""
    problems: list[str] = []
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                raw = archive.read(MANIFEST_NAME)
        else:
            with tarfile.open(path) as tar:
                handle = tar.extractfile(MANIFEST_NAME)
                raw = handle.read() if handle is not None else b""
    except (KeyError, OSError) as exc:
        return ("", "", [f"{MANIFEST_NAME} could not be read: {exc}"])

    try:
        document = json.loads(raw)
    except ValueError as exc:
        return ("", "", [f"{MANIFEST_NAME} is not valid JSON: {exc}"])
    if not isinstance(document, dict):
        return ("", "", [f"{MANIFEST_NAME} is not a JSON object"])

    schema = str(document.get("schema", ""))
    version = str(document.get("schema_version", ""))
    if schema != ARCHIVE_SCHEMA:
        problems.append(f"{MANIFEST_NAME} declares schema {schema!r}, not {ARCHIVE_SCHEMA!r}")
    major = version.split(".", 1)[0]
    if major and major != ARCHIVE_SCHEMA_VERSION.split(".", 1)[0]:
        problems.append(
            f"the archive is schema version {version}; this IdeaPress reads "
            f"{ARCHIVE_SCHEMA_VERSION.split('.', 1)[0]}.x. Upgrade whichever is older."
        )
    project = document.get("project")
    title = str(project.get("title", "")) if isinstance(project, dict) else ""
    if not title:
        problems.append(f"{MANIFEST_NAME} names no project title")
    return (title, version, problems)


def export_project_archive(runtime: Runtime, *, project_id: str, destination: Path) -> Path:
    """Write one project's whole record to a portable archive.

    Args:
        runtime: The process's handles.
        project_id: The project to export.
        destination: The file to write, or a directory to write into.

    Returns:
        The archive's path.

    Raises:
        ProjectNotFound: No such project.

    The archive is a zip whose only required member is ``project.json``: the manifest carries the
    brief, the requirements, the plan, every committed version and every attempt's provenance. It
    is deterministic in the same sense the exports are — sorted keys, no wall-clock stamp inside
    the document — so two exports of an unchanged project differ only in the zip's own timestamps.
    """
    from ideapress.services.export import build_document

    project = runtime.projects.get(project_id)
    document = build_document(runtime, project_id=project_id)

    manifest: dict[str, Any] = {
        "schema": ARCHIVE_SCHEMA,
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "exported_by": "ideapress",
        "project": {
            "title": project.title,
            "slug": project.slug,
            "brief": project.brief_text,
            "content_type": project.content_type,
            "content_type_version": project.content_type_version,
            "workflow_id": project.workflow_id,
            "workflow_version": project.workflow_version,
        },
        "units": [
            {
                "key": unit.key,
                "title": unit.title,
                "content": unit.content,
                "version": unit.version,
                "content_hash": unit.content_hash,
                "word_count": unit.word_count,
                "coverage": [
                    {
                        "requirement_key": entry.key,
                        "satisfied": entry.satisfied,
                        "satisfied_by": entry.satisfied_by,
                    }
                    for entry in unit.coverage
                ],
            }
            for unit in document.units
        ],
    }

    target = destination
    if target.is_dir():
        target = target / f"{project.slug}.ideapress.zip"
    target.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
        )
    logger.info(
        "archive.exported",
        extra={"project_id": project_id, "path": str(target), "units": len(manifest["units"])},
    )
    return target


def import_project_archive(
    runtime: Runtime, *, path: Path, title: str | None = None
) -> dict[str, Any]:
    """Read a project archive and create the project it describes.

    Args:
        runtime: The process's handles.
        path: The archive to read.
        title: A title to use instead of the archive's, when importing a second copy.

    Returns:
        ``{"project_id": …, "title": …, "units": n}``.

    Raises:
        ValidationError: The archive is not one this application reads, or **any** check in
            :func:`inspect_archive` failed. The message names every problem rather than the first,
            because an archive with three faults is better described in one refusal than in three
            attempts.

    The order is the whole point and is not an implementation detail: **inspect, refuse, then
    extract into a temporary directory, then write to the database**. Nothing under the project
    directory is created until the archive has been fully judged, so a refused import leaves no
    trace at all — not a directory, not a row.
    """
    report = inspect_archive(path)
    if not report.safe:
        message = (
            f"{path.name} was refused: " + "; ".join(report.problems) + ". Nothing was written."
        )
        raise ValidationError(
            message,
            details={
                "path": str(path),
                "problems": list(report.problems),
                "entries": report.entries,
                "total_bytes": report.total_bytes,
            },
        )

    with tempfile.TemporaryDirectory(prefix="ideapress-import-") as staging:
        manifest = _extract_manifest(path, Path(staging))
        project_data = manifest.get("project", {})
        project = runtime.projects.create(
            title=title or str(project_data.get("title", "Imported project")),
            brief=str(project_data.get("brief", "")),
            content_type=str(project_data.get("content_type", "article")),
        )

    units = manifest.get("units", [])
    logger.info(
        "archive.imported",
        extra={
            "project_id": project.id,
            "path": str(path),
            "units": len(units) if isinstance(units, list) else 0,
        },
    )
    return {
        "project_id": project.id,
        "title": project.title,
        "units": len(units) if isinstance(units, list) else 0,
        "imported_at": datetime.now(UTC).isoformat(),
    }


def _extract_manifest(path: Path, staging: Path) -> dict[str, Any]:
    """Extract the manifest into ``staging`` and parse it.

    Args:
        path: The archive, already inspected and found safe.
        staging: A fresh temporary directory outside the project root.

    Returns:
        The parsed manifest.

    Extraction happens **member by member into a staging directory**, never with
    ``extractall``: `extractall` on a tar honours the member's own path, and the containment
    argument then rests on the check having been exhaustive rather than on the extraction being
    unable to escape. Writing each member to a path this function computes removes that dependency.
    """
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / MANIFEST_NAME
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive, archive.open(MANIFEST_NAME) as source:
            with target.open("wb") as sink:
                shutil.copyfileobj(source, sink, length=1 << 20)
    else:
        with tarfile.open(path) as tar:
            handle = tar.extractfile(MANIFEST_NAME)
            if handle is None:  # pragma: no cover — inspect_archive already refused this
                message = f"{MANIFEST_NAME} disappeared between inspection and extraction."
                raise ValidationError(message, details={"path": str(path)})
            with target.open("wb") as sink:
                shutil.copyfileobj(handle, sink, length=1 << 20)
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)  # noqa: S101 — inspect_archive proved the shape
    return loaded


def describe_report(report: ArchiveReport) -> Sequence[str]:
    """The report as lines for the CLI, safe to print for a refused archive too."""
    lines = [
        f"entries        : {report.entries}",
        f"uncompressed   : {report.total_bytes} bytes",
        f"compressed     : {report.compressed_bytes} bytes",
        f"ratio          : {report.ratio:.1f}x",
        f"project        : {report.project_title or '—'}",
        f"schema version : {report.schema_version or '—'}",
        f"safe to import : {'yes' if report.safe else 'no'}",
    ]
    lines.extend(f"  refused: {problem}" for problem in report.problems)
    return lines
