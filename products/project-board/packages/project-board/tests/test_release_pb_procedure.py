"""The pb set's one-command release (W364).

Operator, 2026-09-26: "simple release procedure for us". The set
(app-foundation, service-foundation, connection-hub, project-board) is
released at one version by `scripts/release-pb`: prepare sets the version in
every file and gates each package the way the publish workflow does, publish
tags the release merge and runs the workflow once for the whole set, verify
checks PyPI and a clean install. The 2026.09.26.2205 attempt published the
foundations and stopped at connection-hub; these tests keep that from
recurring silently.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
SCRIPT = REPOSITORY_ROOT / "scripts" / "release_pb.py"
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "publish-python-package.yml"


def _script():
    spec = importlib.util.spec_from_file_location("release_pb", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_pb"] = module
    spec.loader.exec_module(module)
    return module


release_pb = _script()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _copy_of_the_set(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative in release_pb.VERSION_ROOTS:
        source = REPOSITORY_ROOT / relative
        target = root / relative
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("node_modules", "__pycache__", "*.egg-info", "build", "dist"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "set")
    return root


def test_prepare_sets_the_new_version_everywhere_and_leaves_no_old_one(tmp_path: Path) -> None:
    root = _copy_of_the_set(tmp_path)
    current = release_pb.read_version(root)
    new = "2099.01.02.0304"

    changed = release_pb.apply_version(root, current, new, "What changed.\n\n- one\n- two")

    assert release_pb.version_files(root, current) == []
    names = {str(path.relative_to(root)) for path in changed}
    for package in release_pb.SET:
        assert f"{package.path}/pyproject.toml" in names
        pyproject = (root / package.path / "pyproject.toml").read_text(encoding="utf-8")
        assert f'version = "{new}"' in pyproject
    # The set depends on itself at the new version, and the chain test moves with it.
    project_board = (root / "products/project-board/packages/project-board/pyproject.toml").read_text(encoding="utf-8")
    assert f"connection-hub[client]>={new}" in project_board
    chain = (root / "products/project-board/packages/project-board/tests/test_release_dependency_chain.py").read_text(encoding="utf-8")
    assert f'RELEASE_VERSION = "{new}"' in chain
    for record in release_pb.RELEASE_RECORDS:
        data = yaml.safe_load((root / record).read_text(encoding="utf-8"))
        top = data.get("package") or data.get("product")
        assert top["ref"] == new, record
        assert top["description"] == "What changed.\n\n- one\n- two\n", record
    product = yaml.safe_load((root / "products/connection-hub/release.yaml").read_text(encoding="utf-8"))
    assert product["config"]["version"] == new
    assert product["components"]["python_package"]["version"] == new


def test_a_version_must_be_the_calendar_form_and_newer() -> None:
    release_pb.check_new_version("2026.09.26.2220", "2026.09.27.0100")
    with pytest.raises(release_pb.ReleaseError, match="YYYY.MM.DD.HHMM"):
        release_pb.check_new_version("2026.09.26.2220", "2026.9.27.100")
    with pytest.raises(release_pb.ReleaseError, match="not newer"):
        release_pb.check_new_version("2026.09.26.2220", "2026.09.26.2220")
    assert release_pb.canonical("2026.09.27.0100") == "2026.9.27.100"


def test_publish_tags_the_merge_that_brought_the_version_not_later_main(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    pyproject = root / "products/project-board/packages/project-board/pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    _git(root.parent, "init", "-q", "-b", "main", str(root))

    def commit(version: str, message: str) -> str:
        pyproject.write_text(f'[project]\nname = "project-board"\nversion = "{version}"\n', encoding="utf-8")
        (root / "other.txt").write_text(message, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", message)
        return _git(root, "rev-parse", "HEAD")

    commit("2026.09.26.2220", "before")
    release = commit("2026.09.27.0100", "release")
    commit("2026.09.27.0100", "later work on main")

    assert release_pb.release_commit("2026.09.27.0100", cwd=root, ref="main") == release
    with pytest.raises(release_pb.ReleaseError, match="does not carry"):
        release_pb.release_commit("2026.09.28.0100", cwd=root, ref="main")


def test_the_workflow_publishes_the_set_in_order_and_stops_at_a_failure() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on") or workflow.get(True)
    assert "pb-set" in triggers["workflow_dispatch"]["inputs"]["package"]["options"]
    job = workflow["jobs"]["build-and-publish"]
    assert job["strategy"]["max-parallel"] == 1
    assert job["strategy"]["fail-fast"] is True
    matrix = job["strategy"]["matrix"]["package"]
    order = '","'.join(package.name for package in release_pb.SET)
    assert f'\'["{order}"]\'' in matrix
    steps = [step["name"] for step in job["steps"] if "name" in step]
    assert steps.index("Wait for first-party dependencies of this version on the index") < steps.index("Test package")
    assert steps[-1] == "Publish to PyPI"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "${{ inputs.package }}" not in text.split("jobs:", 1)[1].replace(
        "format('[\"{0}\"]', inputs.package)", ""
    ).replace("inputs.package == 'pb-set'", ""), "steps read the matrix package"
    for package in release_pb.SET:
        assert f'"{package.path}"' in text and f'"{package.import_name}"' in text, package.name


def test_the_release_guide_leads_with_the_procedure() -> None:
    guide = (REPOSITORY_ROOT / "docs" / "releases.md").read_text(encoding="utf-8")
    first_section = guide.split("\n## ", 2)[1]
    assert first_section.startswith("Release the pb set")
    for command in ("scripts/release-pb prepare", "scripts/release-pb publish", "scripts/release-pb verify",
                    "pb source use-release --expect-version"):
        assert command in first_section, command
