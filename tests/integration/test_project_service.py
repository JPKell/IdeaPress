"""Project lifecycle over a real migrated database."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from baseaicore import ValidationError

from ideapress.errors import ProjectNotFound
from ideapress.services.database import Database, upgrade
from ideapress.services.projects import ProjectService

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def service(tmp_path: Path) -> Iterator[ProjectService]:
    database = Database.from_url(f"sqlite:///{tmp_path / 'ideapress.sqlite3'}")
    upgrade(database)
    yield ProjectService(database, project_dir=tmp_path / "projects")
    database.close()


def test_create_then_reopen_across_a_new_handle(tmp_path: Path) -> None:
    """P1 AC2: a project can be created and reopened across a restart."""
    url = f"sqlite:///{tmp_path / 'ideapress.sqlite3'}"
    first = Database.from_url(url)
    upgrade(first)
    created = ProjectService(first, project_dir=tmp_path / "projects").create(title="A study")
    first.close()

    second = Database.from_url(url)
    reopened = ProjectService(second, project_dir=tmp_path / "projects").get(created.id)
    second.close()
    assert reopened.title == "A study"
    assert reopened.slug == created.slug


def test_slug_collisions_are_suffixed_never_overwritten(service: ProjectService) -> None:
    slugs = [service.create(title="Same title").slug for _ in range(3)]
    assert slugs == ["same-title", "same-title-2", "same-title-3"]
    assert len(set(slugs)) == 3


def test_a_traversal_title_creates_a_contained_directory(service: ProjectService) -> None:
    project = service.create(title="../../etc/passwd")
    directory = service.directory(project)
    assert directory.is_dir()
    assert directory.parent.name == "projects"
    assert "etc-passwd" == directory.name


def test_the_project_directory_is_private(service: ProjectService) -> None:
    """Likely failure mode: a data directory created with permissive modes."""
    directory = service.directory(service.create(title="Private work"))
    assert directory.stat().st_mode & 0o077 == 0


def test_an_empty_title_is_refused(service: ProjectService) -> None:
    with pytest.raises(ValidationError):
        service.create(title="   ")


def test_an_overlong_title_is_refused(service: ProjectService) -> None:
    with pytest.raises(ValidationError) as caught:
        service.create(title="x" * 201)
    assert "201" in caught.value.message


def test_missing_project_raises_the_documented_error(service: ProjectService) -> None:
    with pytest.raises(ProjectNotFound) as caught:
        service.get("01ZZZZZZZZZZZZZZZZZZZZZZZZ")
    assert caught.value.code == "PROJECT_NOT_FOUND"


def test_update_changes_the_brief_but_never_the_slug(service: ProjectService) -> None:
    project = service.create(title="First title", brief="one")
    updated = service.update(project.id, title="A completely different title", brief="two")
    assert updated.title == "A completely different title"
    assert updated.brief_text == "two"
    assert updated.slug == project.slug, "the slug names a directory holding the user's exports"


def test_update_refuses_an_unknown_status(service: ProjectService) -> None:
    project = service.create(title="A study")
    with pytest.raises(ValidationError) as caught:
        service.update(project.id, status="finished")
    assert "finished" in caught.value.message


def test_archive_hides_without_removing(service: ProjectService) -> None:
    project = service.create(title="Done with this")
    service.archive(project.id)
    assert [p.id for p in service.list()] == []
    assert [p.id for p in service.list(include_archived=True)] == [project.id]
    assert service.get(project.id).status == "archived"


def test_unarchive_restores_it(service: ProjectService) -> None:
    project = service.create(title="Back again")
    service.archive(project.id)
    service.unarchive(project.id)
    assert [p.id for p in service.list()] == [project.id]


def test_delete_previews_exactly_what_it_removes(service: ProjectService) -> None:
    project = service.create(title="To remove")
    directory = service.directory(project)
    (directory / "export.md").write_text("content", encoding="utf-8")

    preview = service.preview_delete(project.id)
    assert preview.project.id == project.id
    assert preview.directory == directory
    assert preview.directory_bytes == len("content")
    assert directory.is_dir(), "a preview removes nothing"


def test_delete_without_confirmation_removes_nothing(service: ProjectService) -> None:
    project = service.create(title="Still here")
    service.delete(project.id, confirm=False)
    assert service.get(project.id).id == project.id
    assert service.directory(project).is_dir()


def test_confirmed_delete_removes_rows_and_directory(service: ProjectService) -> None:
    project = service.create(title="Gone")
    directory = service.directory(project)
    service.delete(project.id, confirm=True)
    with pytest.raises(ProjectNotFound):
        service.get(project.id)
    assert not directory.exists()


def test_list_orders_by_most_recent_activity(service: ProjectService) -> None:
    first = service.create(title="Older")
    second = service.create(title="Newer")
    service.update(first.id, brief="touched")
    assert [p.id for p in service.list()] == [first.id, second.id]


def test_list_filters_by_content_type(service: ProjectService) -> None:
    service.create(title="An article", content_type="article")
    report = service.create(title="A report", content_type="report")
    assert [p.id for p in service.list(content_type="report")] == [report.id]


def test_a_service_rooted_elsewhere_refuses_an_escaping_slug(tmp_path: Path) -> None:
    """The containment check is not the slug check: both have to hold."""
    database = Database.from_url(f"sqlite:///{tmp_path / 'ip.sqlite3'}")
    upgrade(database)
    service = ProjectService(database, project_dir=tmp_path / "projects")
    with pytest.raises(ValidationError):
        service._directory_for("../escape")  # noqa: SLF001 — the guard is the unit under test
    with pytest.raises(ValidationError):
        service._directory_for("a/b")  # noqa: SLF001
    database.close()
