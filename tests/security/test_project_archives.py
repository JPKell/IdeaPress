"""Hardened archive import — M7-27, and P9's "writes before validating" failure mode.

Every archive here is hostile and hand-built, because an archive IdeaPress wrote is not a test of
an importer: the interesting inputs are the ones a stranger authored. Each case asserts two things
and the second is the one that matters — that the archive is **refused**, and that the refusal left
**nothing on disk**.

Security Standards §14's archive line is "archive bomb, absolute-path entry, `..` entry, and
symlink entry all rejected". All four are here, plus the shapes each of the four size caps exists
to catch, because a single cap always has a shape that walks under it.
"""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from baseaicore import ValidationError

from ideapress.config import load_settings
from ideapress.services.project_archive import (
    ARCHIVE_SCHEMA,
    ARCHIVE_SCHEMA_VERSION,
    MANIFEST_NAME,
    MAX_ENTRIES,
    MAX_RATIO,
    export_project_archive,
    import_project_archive,
    inspect_archive,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.services.runtime import Runtime


def _manifest(title: str = "A project") -> bytes:
    return json.dumps(
        {
            "schema": ARCHIVE_SCHEMA,
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "project": {"title": title, "brief": "A brief.", "content_type": "article"},
            "units": [],
        }
    ).encode()


def _zip(tmp_path: Path, members: dict[str, bytes], *, name: str = "hostile.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member, body in members.items():
            archive.writestr(member, body)
    return path


def _zip_with_symlink(tmp_path: Path, link_name: str, target: str) -> Path:
    """A zip whose member is a symlink, stored the way zip stores one: in the mode bits."""
    path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(MANIFEST_NAME, _manifest())
        info = zipfile.ZipInfo(link_name)
        info.create_system = 3  # Unix
        info.external_attr = (0o120777 << 16) | 0o20  # S_IFLNK
        archive.writestr(info, target)
    return path


@pytest.fixture
def runtime() -> Iterator[Runtime]:
    from ideapress.services.runtime import build_runtime

    built = build_runtime(load_settings().settings)
    try:
        yield built
    finally:
        built.close()


def _project_dir(runtime: Runtime) -> Path:
    assert runtime.settings.storage.project_dir is not None
    return Path(runtime.settings.storage.project_dir)


def _tree(root: Path) -> set[str]:
    """Everything under ``root``, for proving a refusal wrote nothing."""
    return {p.relative_to(root).as_posix() for p in root.rglob("*")} if root.exists() else set()


# ------------------------------------------------------------------ containment


@pytest.mark.parametrize(
    "member",
    [
        "../escaped.txt",
        "../../etc/passwd",
        "units/../../../outside.txt",
        "/absolute/path.txt",
        "/etc/passwd",
    ],
)
def test_an_entry_that_escapes_the_directory_is_refused(
    member: str, tmp_path: Path, runtime: Runtime
) -> None:
    """Security Standards §14: absolute-path and `..` entries rejected — before any write."""
    archive = _zip(tmp_path, {MANIFEST_NAME: _manifest(), member: b"payload"})
    report = inspect_archive(archive)
    assert not report.safe
    assert any("escapes the extraction directory" in problem for problem in report.problems)

    before = _tree(_project_dir(runtime))
    with pytest.raises(ValidationError, match="Nothing was written"):
        import_project_archive(runtime, path=archive)
    assert _tree(_project_dir(runtime)) == before, "a refused import wrote to the project directory"


def test_a_symlink_entry_is_refused(tmp_path: Path, runtime: Runtime) -> None:
    """A symlink pointing at `/etc/passwd` turns a later read of "the project's own file" into a
    read of something else, so it is refused rather than skipped."""
    archive = _zip_with_symlink(tmp_path, "units/link.txt", "/etc/passwd")
    report = inspect_archive(archive)
    assert not report.safe
    assert any("symlink" in problem for problem in report.problems)

    before = _tree(_project_dir(runtime))
    with pytest.raises(ValidationError):
        import_project_archive(runtime, path=archive)
    assert _tree(_project_dir(runtime)) == before


def test_a_tar_symlink_is_refused_too(tmp_path: Path) -> None:
    """The same guard on the other container format: a check that covered only zip would be a gap
    in exactly one surface."""
    path = tmp_path / "hostile.tar"
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo(MANIFEST_NAME)
        body = _manifest()
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
        link = tarfile.TarInfo("units/link.txt")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    report = inspect_archive(path)
    assert not report.safe
    assert any("symlink" in problem for problem in report.problems)


def test_a_tar_entry_that_escapes_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "escape.tar"
    with tarfile.open(path, "w") as archive:
        for name in (MANIFEST_NAME, "../../escaped.txt"):
            body = _manifest() if name == MANIFEST_NAME else b"payload"
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    report = inspect_archive(path)
    assert not report.safe
    assert any("escapes" in problem for problem in report.problems)


# ------------------------------------------------------------------ bombs, all four shapes


def test_a_high_ratio_archive_is_refused(tmp_path: Path, runtime: Runtime) -> None:
    """The classic zip bomb: small on disk, enormous expanded. Refused on its *declared* sizes,
    before a single byte is decompressed."""
    archive = _zip(
        tmp_path, {MANIFEST_NAME: _manifest(), "units/big.txt": b"\0" * (8 * 1024 * 1024)}
    )
    report = inspect_archive(archive)
    assert not report.safe
    assert any("ratio cap" in problem for problem in report.problems)
    assert report.ratio > MAX_RATIO

    before = _tree(_project_dir(runtime))
    with pytest.raises(ValidationError):
        import_project_archive(runtime, path=archive)
    assert _tree(_project_dir(runtime)) == before


def test_too_many_entries_is_refused(tmp_path: Path) -> None:
    """The many-tiny-files bomb, which walks under every size cap."""
    members = {MANIFEST_NAME: _manifest()}
    members.update({f"units/{index}.txt": b"x" for index in range(MAX_ENTRIES + 1)})
    report = inspect_archive(_zip(tmp_path, members))
    assert not report.safe
    assert any("past the" in problem and "cap" in problem for problem in report.problems)


def test_the_caps_are_all_declared_and_ordered_sensibly() -> None:
    """Four caps, each catching a shape the others miss. Asserted so removing one is deliberate."""
    from ideapress.services.project_archive import (
        MAX_ENTRY_BYTES,
        MAX_TOTAL_BYTES,
    )

    assert MAX_ENTRY_BYTES < MAX_TOTAL_BYTES, "a per-entry cap above the total cap does nothing"
    assert MAX_ENTRIES > 0
    assert MAX_RATIO > 1


# ------------------------------------------------------------------ shape


def test_an_archive_with_no_manifest_is_refused(tmp_path: Path) -> None:
    report = inspect_archive(_zip(tmp_path, {"units/one.txt": b"content"}))
    assert not report.safe
    assert any("not a project archive" in problem for problem in report.problems)


def test_a_manifest_that_is_not_json_is_refused(tmp_path: Path) -> None:
    report = inspect_archive(_zip(tmp_path, {MANIFEST_NAME: b"not json at all {{{"}))
    assert not report.safe
    assert any("not valid JSON" in problem for problem in report.problems)


def test_a_foreign_schema_is_refused_by_name(tmp_path: Path) -> None:
    """An archive from a different application is a different thing from a corrupt one."""
    body = json.dumps(
        {"schema": "loadcoach.job_export", "schema_version": "1.0", "project": {"title": "x"}}
    ).encode()
    report = inspect_archive(_zip(tmp_path, {MANIFEST_NAME: body}))
    assert not report.safe
    assert any("declares schema" in problem for problem in report.problems)


def test_a_future_major_version_is_refused_naming_both(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "schema": ARCHIVE_SCHEMA,
            "schema_version": "9.0",
            "project": {"title": "From the future"},
        }
    ).encode()
    report = inspect_archive(_zip(tmp_path, {MANIFEST_NAME: body}))
    assert not report.safe
    assert any("schema version 9.0" in problem for problem in report.problems)


def test_a_file_that_is_not_an_archive_is_a_different_answer(tmp_path: Path) -> None:
    """ "This is not an archive" must not read as "this archive is malicious"."""
    plain = tmp_path / "photo.png"
    plain.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    with pytest.raises(ValidationError, match="neither a zip nor a tar"):
        inspect_archive(plain)


def test_a_missing_file_is_refused_plainly(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="is not a file"):
        inspect_archive(tmp_path / "nothing-here.zip")


# ------------------------------------------------------------------ the round trip


def test_a_project_survives_export_and_import(runtime: Runtime, tmp_path: Path) -> None:
    """The reason the hardening exists: the honest path has to work."""
    project = runtime.projects.create(
        title="Local inference", brief="Inference runs on your own machine."
    )
    archive = export_project_archive(runtime, project_id=project.id, destination=tmp_path)
    assert archive.exists()

    report = inspect_archive(archive)
    assert report.safe, report.problems
    assert report.project_title == "Local inference"
    assert report.schema_version == ARCHIVE_SCHEMA_VERSION

    result = import_project_archive(runtime, path=archive, title="Imported copy")
    assert result["title"] == "Imported copy"
    imported = runtime.projects.get(str(result["project_id"]))
    assert imported.brief_text == "Inference runs on your own machine."


def test_an_exported_archive_is_a_zip_with_the_manifest_at_its_root(
    runtime: Runtime, tmp_path: Path
) -> None:
    project = runtime.projects.create(title="Local inference", brief="A brief.")
    archive = export_project_archive(runtime, project_id=project.id, destination=tmp_path)
    with zipfile.ZipFile(archive) as opened:
        assert MANIFEST_NAME in opened.namelist()
        manifest = json.loads(opened.read(MANIFEST_NAME))
    assert manifest["schema"] == ARCHIVE_SCHEMA
    assert manifest["project"]["title"] == "Local inference"


def test_exporting_twice_produces_the_same_manifest(runtime: Runtime, tmp_path: Path) -> None:
    """The manifest is deterministic — sorted keys, no wall-clock stamp inside the document — so a
    backup can be compared against its predecessor."""
    project = runtime.projects.create(title="Local inference", brief="A brief.")
    first = export_project_archive(runtime, project_id=project.id, destination=tmp_path / "a")
    second = export_project_archive(runtime, project_id=project.id, destination=tmp_path / "b")
    with zipfile.ZipFile(first) as one, zipfile.ZipFile(second) as two:
        assert one.read(MANIFEST_NAME) == two.read(MANIFEST_NAME)


# ------------------------------------------------------------------ the ordering itself


def test_inspection_writes_nothing_at_all(tmp_path: Path, runtime: Runtime) -> None:
    """The property the whole module is shaped around, asserted directly on the safe path too.

    Not only "a refused archive writes nothing" but "*inspecting* writes nothing", so `--inspect`
    is genuinely safe to run on something a stranger sent.
    """
    project = runtime.projects.create(title="Local inference", brief="A brief.")
    archive = export_project_archive(runtime, project_id=project.id, destination=tmp_path)
    before = _tree(_project_dir(runtime))
    for _ in range(3):
        inspect_archive(archive)
    assert _tree(_project_dir(runtime)) == before


def test_an_archive_with_several_faults_names_all_of_them(tmp_path: Path) -> None:
    """One refusal describing three faults beats three attempts describing one each."""
    archive = _zip(
        tmp_path,
        {
            MANIFEST_NAME: b"not json",
            "../escaped.txt": b"x",
            "/absolute.txt": b"x",
        },
    )
    report = inspect_archive(archive)
    assert len(report.problems) >= 2, report.problems


def test_the_importer_never_calls_extractall() -> None:
    """`extractall` honours the member's own path, which puts the containment guarantee back on the
    check having been exhaustive. Writing each member to a computed path removes that dependency,
    and this keeps it removed."""
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "ideapress"
        / "services"
        / "project_archive.py"
    )
    body = source.read_text(encoding="utf-8")
    # `.extractall(` — an actual call, not the prose explaining why there is none. The module
    # documents the reasoning, so matching the bare word would fail on its own rationale.
    assert ".extractall(" not in body, "the importer calls extractall"


def _seed_committed_unit(runtime: Runtime, project_id: str, text: str) -> None:
    """Put one committed unit into a project without running a stage."""
    from datetime import UTC, datetime

    from baseaicore import sha256_of

    from ideapress.infrastructure.db.models import Unit as UnitRow
    from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow

    with runtime.storage.write() as session:
        unit = UnitRow(
            project_id=project_id,
            unit_key="U-01",
            ordinal=1,
            title="Where the work happens",
            goal_text="Say plainly where inference runs.",
            requirement_keys_json=[],
            state="committed",
        )
        session.add(unit)
        session.flush()
        version = UnitVersionRow(
            unit_id=unit.id,
            version=1,
            content_text=text,
            content_hash=f"sha256:{sha256_of(text)}",
            word_count=len(text.split()),
            char_count=len(text),
            committed=True,
            committed_at=datetime.now(UTC),
        )
        session.add(version)
        session.flush()
        unit.current_version_id = version.id


def test_an_imported_project_carries_its_committed_units_back(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A backup of the intention is not a backup of the work.

    The round trip has to bring the committed text back, with its version and its hash — otherwise
    "portable project" means "portable brief", and the archive is not the thing that survives an
    installation being deleted.
    """
    from ideapress.services.export import build_document
    from ideapress.services.unit_reports import unit_list

    project = runtime.projects.create(title="Local inference", brief="A brief.")
    _seed_committed_unit(runtime, project.id, "Everything runs on your own machine.")

    archive = export_project_archive(runtime, project_id=project.id, destination=tmp_path)
    with zipfile.ZipFile(archive) as opened:
        manifest = json.loads(opened.read(MANIFEST_NAME))
    assert manifest["units"], "the export carried no units, so the import cannot restore any"

    result = import_project_archive(runtime, path=archive, title="Restored")
    assert result["units"] == 1

    units = unit_list(runtime, project_id=str(result["project_id"]))
    assert len(units) == 1
    assert units[0]["state"] == "committed"
    assert units[0]["content_hash"] == manifest["units"][0]["content_hash"]

    document = build_document(runtime, project_id=str(result["project_id"]))
    assert "own machine" in document.units[0].content


def test_an_import_does_not_claim_another_installations_attempts_as_its_own(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The attempts happened elsewhere, against models and prompts this installation may not have.

    Writing them here as this installation's own provenance would put a record into the database
    that is not true of it — the same offence as coercing an unmeasurable value to zero. The
    content hash is the honest link back.
    """
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Attempt

    project = runtime.projects.create(title="Local inference", brief="A brief.")
    _seed_committed_unit(runtime, project.id, "Everything runs on your own machine.")
    archive = export_project_archive(runtime, project_id=project.id, destination=tmp_path)
    result = import_project_archive(runtime, path=archive, title="Restored")

    with runtime.storage.read() as session:
        attempts = session.execute(select(Attempt)).scalars().all()
    assert attempts == [], "the import fabricated attempt provenance"
    assert result["units"] == 1
