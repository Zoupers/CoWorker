from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coworker.container_workspace import (
    DEFAULT_REPOSITORY_URL,
    WorkspaceSettings,
    _attach_state_directory,
    _run_git,
    initialize_workspace,
)


def _settings(tmp_path: Path, repository_url: str = "") -> WorkspaceSettings:
    return WorkspaceSettings(
        workspace_path=tmp_path / "workspace",
        state_path=tmp_path / "state",
        source_path=tmp_path / "source",
        repository_url=repository_url,
        repository_ref="",
    )


def _create_repository(repository_path: Path) -> None:
    repository_path.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repository_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repository_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repository_path,
        check=True,
    )
    (repository_path / "README.md").write_text("test repository\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repository_path,
        check=True,
        capture_output=True,
    )


def test_official_repository_uses_image_revision() -> None:
    settings = WorkspaceSettings.from_environment(
        {
            "COWORKER_REPOSITORY_URL": DEFAULT_REPOSITORY_URL,
            "COWORKER_IMAGE_REVISION": "abc123",
        }
    )

    assert settings.repository_ref == "abc123"


def test_custom_repository_does_not_assume_official_revision() -> None:
    settings = WorkspaceSettings.from_environment(
        {
            "COWORKER_REPOSITORY_URL": "git@example.com:team/coworker.git",
            "COWORKER_IMAGE_REVISION": "abc123",
        }
    )

    assert settings.repository_ref == ""


def test_explicit_repository_ref_overrides_image_revision() -> None:
    settings = WorkspaceSettings.from_environment(
        {
            "COWORKER_REPOSITORY_URL": DEFAULT_REPOSITORY_URL,
            "COWORKER_REPOSITORY_REF": "release-candidate",
            "COWORKER_IMAGE_REVISION": "abc123",
        }
    )

    assert settings.repository_ref == "release-candidate"


def test_clone_initializes_git_workspace(tmp_path: Path, mocker) -> None:
    repository_path = tmp_path / "repository"
    _create_repository(repository_path)
    settings = _settings(tmp_path, str(repository_path))
    attach_state = mocker.patch("coworker.container_workspace._attach_state_directory")

    initialize_workspace(settings)

    assert (settings.workspace_path / ".git").is_dir()
    assert (settings.workspace_path / "README.md").read_text(encoding="utf-8") == (
        "test repository\n"
    )
    attach_state.assert_called_once_with(settings)


def test_seed_initializes_local_git_baseline(tmp_path: Path, mocker) -> None:
    settings = _settings(tmp_path)
    settings.source_path.mkdir()
    (settings.source_path / "README.md").write_text("bundled source\n", encoding="utf-8")
    attach_state = mocker.patch("coworker.container_workspace._attach_state_directory")

    initialize_workspace(settings)

    assert (settings.workspace_path / ".git").is_dir()
    assert (settings.workspace_path / "README.md").is_file()
    attach_state.assert_called_once_with(settings)


def test_non_empty_destination_without_git_is_preserved(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.workspace_path.mkdir()
    existing_file = settings.workspace_path / "keep.txt"
    existing_file.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Refusing to initialize non-empty workspace"):
        initialize_workspace(settings)

    assert existing_file.read_text(encoding="utf-8") == "keep me\n"


def test_repository_url_with_embedded_credentials_is_rejected(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "https://token@example.com/team/coworker.git")

    with pytest.raises(RuntimeError, match="must not contain credentials"):
        initialize_workspace(settings)

    assert not settings.workspace_path.exists()


def test_existing_git_workspace_is_not_recloned(tmp_path: Path, mocker) -> None:
    settings = _settings(tmp_path, "https://example.com/replacement.git")
    (settings.workspace_path / ".git").mkdir(parents=True)
    existing_file = settings.workspace_path / "local-change.txt"
    existing_file.write_text("preserve me\n", encoding="utf-8")
    run_git = mocker.patch("coworker.container_workspace._run_git")
    mocker.patch("coworker.container_workspace._attach_state_directory")

    initialize_workspace(settings)

    run_git.assert_not_called()
    assert existing_file.read_text(encoding="utf-8") == "preserve me\n"


def test_failed_checkout_leaves_no_partial_workspace(tmp_path: Path, mocker) -> None:
    settings = _settings(tmp_path, "https://example.com/coworker.git")
    settings = WorkspaceSettings(
        workspace_path=settings.workspace_path,
        state_path=settings.state_path,
        source_path=settings.source_path,
        repository_url=settings.repository_url,
        repository_ref="missing-ref",
    )

    def fail_after_clone(command, secret=""):
        if "clone" in command:
            Path(command[-1]).mkdir(parents=True)
            return
        raise RuntimeError("checkout failed")

    mocker.patch("coworker.container_workspace._run_git", side_effect=fail_after_clone)

    with pytest.raises(RuntimeError, match="checkout failed"):
        initialize_workspace(settings)

    assert not settings.workspace_path.exists()
    assert not list(tmp_path.glob(".coworker-workspace-*"))


def test_state_directory_is_attached_as_workspace_data(tmp_path: Path, mocker) -> None:
    settings = _settings(tmp_path)
    settings.workspace_path.mkdir()
    symlink_to = mocker.patch.object(Path, "symlink_to")

    _attach_state_directory(settings)

    symlink_to.assert_called_once_with(settings.state_path, target_is_directory=True)


def test_git_error_redacts_repository_url(monkeypatch) -> None:
    repository_url = "https://token@example.com/team/coworker.git"
    failed_result = MagicMock(
        returncode=128,
        stderr=f"fatal: unable to access '{repository_url}'",
        stdout="",
    )
    monkeypatch.setattr(subprocess, "run", MagicMock(return_value=failed_result))

    with pytest.raises(RuntimeError) as raised:
        _run_git(["git", "clone", repository_url, "workspace"], repository_url)

    assert repository_url not in str(raised.value)
    assert "<repository-url>" in str(raised.value)
