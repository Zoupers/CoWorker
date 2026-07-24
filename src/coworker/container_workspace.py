from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_REPOSITORY_URL = "https://github.com/VirtualBeingsResearch/CoWorker.git"
DEFAULT_WORKSPACE_PATH = Path("/workspace/CoWorker")
DEFAULT_STATE_PATH = Path("/var/lib/coworker")
DEFAULT_SOURCE_PATH = Path("/app")
GIT_COMMAND_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class WorkspaceSettings:
    workspace_path: Path
    state_path: Path
    source_path: Path
    repository_url: str
    repository_ref: str

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> WorkspaceSettings:
        values = os.environ if environment is None else environment
        repository_url = values.get("COWORKER_REPOSITORY_URL", DEFAULT_REPOSITORY_URL).strip()
        requested_ref = values.get("COWORKER_REPOSITORY_REF", "").strip()
        image_revision = values.get("COWORKER_IMAGE_REVISION", "").strip()
        repository_ref = requested_ref
        if not repository_ref and repository_url == DEFAULT_REPOSITORY_URL:
            repository_ref = image_revision
        return cls(
            workspace_path=Path(
                values.get("COWORKER_WORKSPACE_PATH", str(DEFAULT_WORKSPACE_PATH))
            ),
            state_path=Path(values.get("COWORKER_STATE_PATH", str(DEFAULT_STATE_PATH))),
            source_path=Path(values.get("COWORKER_SOURCE_PATH", str(DEFAULT_SOURCE_PATH))),
            repository_url=repository_url,
            repository_ref=repository_ref,
        )


def initialize_workspace(settings: WorkspaceSettings) -> None:
    workspace_path = settings.workspace_path
    if (workspace_path / ".git").exists():
        print(f"Using existing Coworker workspace at {workspace_path}", flush=True)
        _attach_state_directory(settings)
        return

    _require_empty_destination(workspace_path)
    if settings.repository_url:
        _clone_repository(settings)
    else:
        _seed_workspace_from_image(settings)
    _attach_state_directory(settings)


def _require_empty_destination(workspace_path: Path) -> None:
    if not workspace_path.exists():
        return
    if not workspace_path.is_dir():
        raise RuntimeError(f"Workspace path is not a directory: {workspace_path}")
    if any(workspace_path.iterdir()):
        raise RuntimeError(
            f"Refusing to initialize non-empty workspace without .git: {workspace_path}"
        )
    workspace_path.rmdir()


def _clone_repository(settings: WorkspaceSettings) -> None:
    _reject_embedded_http_credentials(settings.repository_url)
    workspace_path = settings.workspace_path
    print(
        f"Cloning Coworker workspace into {workspace_path}"
        f"{_ref_message(settings.repository_ref)}",
        flush=True,
    )
    with _staged_workspace(workspace_path) as staged_workspace:
        _run_git(
            ["git", "clone", settings.repository_url, str(staged_workspace)],
            settings.repository_url,
        )
        if settings.repository_ref:
            _run_git(
                [
                    "git",
                    "-C",
                    str(staged_workspace),
                    "checkout",
                    settings.repository_ref,
                ]
            )


def _ref_message(repository_ref: str) -> str:
    return f" at {repository_ref}" if repository_ref else ""


def _reject_embedded_http_credentials(repository_url: str) -> None:
    parsed_url = urlsplit(repository_url)
    if parsed_url.scheme in {"http", "https"} and (
        parsed_url.username is not None or parsed_url.password is not None
    ):
        raise RuntimeError(
            "Repository URLs must not contain credentials; use a Git credential helper"
        )


def _seed_workspace_from_image(settings: WorkspaceSettings) -> None:
    source_path = settings.source_path
    if not source_path.is_dir():
        raise RuntimeError(f"Bundled source directory does not exist: {source_path}")

    workspace_path = settings.workspace_path
    print(f"Creating local Git workspace from bundled source at {workspace_path}", flush=True)
    with _staged_workspace(workspace_path) as staged_workspace:
        shutil.copytree(
            source_path,
            staged_workspace,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "data"),
        )
        _run_git(["git", "-C", str(staged_workspace), "init", "-b", "main"])
        _run_git(
            [
                "git",
                "-C",
                str(staged_workspace),
                "config",
                "user.name",
                "Coworker Container",
            ]
        )
        _run_git(
            [
                "git",
                "-C",
                str(staged_workspace),
                "config",
                "user.email",
                "coworker@localhost",
            ]
        )
        _run_git(["git", "-C", str(staged_workspace), "add", "--all"])
        _run_git(
            [
                "git",
                "-C",
                str(staged_workspace),
                "commit",
                "-m",
                "chore: initialize container workspace",
            ]
        )


@contextmanager
def _staged_workspace(workspace_path: Path) -> Iterator[Path]:
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".coworker-workspace-",
        dir=workspace_path.parent,
    ) as temporary_directory:
        staged_workspace = Path(temporary_directory) / "repository"
        yield staged_workspace
        staged_workspace.replace(workspace_path)


def _attach_state_directory(settings: WorkspaceSettings) -> None:
    state_path = settings.state_path
    state_path.mkdir(parents=True, exist_ok=True)
    data_path = settings.workspace_path / "data"

    if data_path.is_symlink():
        if data_path.resolve() != state_path.resolve():
            raise RuntimeError(f"Workspace data link points outside configured state: {data_path}")
        return
    if data_path.exists():
        if not data_path.is_dir() or any(data_path.iterdir()):
            raise RuntimeError(f"Refusing to replace non-empty workspace data path: {data_path}")
        data_path.rmdir()
    data_path.symlink_to(state_path, target_is_directory=True)


def _run_git(command: Sequence[str], secret: str = "") -> None:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Git command timed out while initializing the workspace") from error
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "unknown Git error").strip()
    if secret:
        detail = detail.replace(secret, "<repository-url>")
    raise RuntimeError(f"Git workspace initialization failed: {detail}")


def main() -> int:
    settings = WorkspaceSettings.from_environment()
    try:
        initialize_workspace(settings)
    except (OSError, RuntimeError) as error:
        print(f"Coworker workspace initialization failed: {error}", file=sys.stderr)
        return 1

    os.chdir(settings.workspace_path)
    command = sys.argv[1:] or ["/opt/venv/bin/coworker"]
    os.execvpe(command[0], command, os.environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
