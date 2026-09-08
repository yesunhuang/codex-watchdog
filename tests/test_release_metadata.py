from __future__ import annotations

import pytest

from scripts.release_metadata import project_version, release_plan


def test_only_canonical_project_version_drives_release():
    assert project_version('[tool.other]\nversion = "9.0.0"\n[project]\nversion = "0.2.3"\n') == "0.2.3"


def test_non_version_pyproject_edit_does_not_republish():
    assert release_plan("0.2.3", "0.2.3", ["v0.2.3"], ["v0.2.3"])["should_release"] == "false"


def test_upgrade_predecessor_uses_version_order_including_beta_releases():
    result = release_plan("0.2.10", "0.2.9", ["v0.2.2", "v0.2.9", "unrelated"], [])
    assert result["previous_tag"] == "v0.2.9"
    assert result["tag"] == "v0.2.10"


@pytest.mark.parametrize("releases,tags", [(["v0.2.3"], []), (["v0.2.2"], ["v0.2.3"])])
def test_existing_release_or_tag_cannot_be_overwritten(releases, tags):
    with pytest.raises(ValueError, match="not be overwritten"):
        release_plan("0.2.3", None, releases, tags)


def test_version_regression_cannot_publish():
    with pytest.raises(ValueError, match="newer"):
        release_plan("0.2.1", None, ["v0.2.2"], [])


def test_missing_upgrade_predecessor_fails_closed():
    with pytest.raises(ValueError, match="previous public release"):
        release_plan("0.2.3", None, [], [])
