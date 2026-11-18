"""Static checks of .github/workflows/docker-publish.yml (CI-1): push only on main, GITHUB_TOKEN only."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / ".github/workflows/docker-publish.yml").read_text()
WF = yaml.safe_load(TEXT)
ON = WF.get("on", WF.get(True))  # PyYAML parses a bare `on:` key as boolean True
BUILD = WF["jobs"]["build"]
PUBLISH = WF["jobs"]["publish"]
STEPS = BUILD["steps"] + PUBLISH["steps"]
SMOKE_TAG = "${{ env.IMAGE_NAME }}:ci-smoke"


def _step(uses_prefix: str, name: str | None = None, steps: list | None = None) -> dict:
    hits = [s for s in (steps or STEPS) if s.get("uses", "").startswith(uses_prefix) and (name is None or s.get("name") == name)]
    assert len(hits) == 1, (uses_prefix, name, hits)
    return hits[0]


def test_triggers():
    assert set(ON) == {"push", "pull_request", "workflow_dispatch"}
    assert ON["push"]["branches"] == ["main"]


def test_permissions_scoped():
    assert WF["permissions"] == {"contents": "read"}
    assert BUILD["permissions"] == {"contents": "read"}  # runs on PRs/branches: no write grant
    assert PUBLISH["permissions"] == {"contents": "read", "packages": "write"}
    assert set(WF["jobs"]) == {"build", "publish"}
    assert [n for n, j in WF["jobs"].items() if "write" in str(j.get("permissions"))] == ["publish"]


def test_only_github_token_secret():
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", TEXT)) == {"GITHUB_TOKEN"}
    login = _step("docker/login-action")
    assert login["with"]["registry"] == "ghcr.io"
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"


def test_publish_only_on_main_non_pr():
    assert PUBLISH["needs"] == "build"
    cond = PUBLISH["if"]
    assert "github.event_name != 'pull_request'" in cond and "github.ref == 'refs/heads/main'" in cond
    assert "if" not in BUILD
    # Nothing in the build job logs in or pushes; no step builds with push: true at all (publish uses docker push).
    assert not any(s.get("uses", "").startswith("docker/login-action") for s in BUILD["steps"])
    assert any(s.get("uses", "").startswith("docker/login-action") for s in PUBLISH["steps"])
    for s in STEPS:
        if s.get("uses", "").startswith("docker/build-push-action"):
            assert s["with"]["push"] is False
        assert "docker push" not in "".join(x.get("run", "") for x in BUILD["steps"])


def test_image_name_and_tags():
    name = WF["env"]["IMAGE_NAME"]
    assert name == "ghcr.io/hue2/tcg-price-watch" and name == name.lower()
    meta = _step("docker/metadata-action", steps=PUBLISH["steps"])["with"]
    assert meta["images"] == "${{ env.IMAGE_NAME }}"
    assert "type=raw,value=latest" in meta["tags"]
    assert "type=raw,value=${{ steps.ver.outputs.version }}" in meta["tags"]
    assert "type=sha,prefix=sha-,format=short" in meta["tags"]
    labels = _step("docker/metadata-action", steps=BUILD["steps"])["with"]["labels"]  # baked at build time
    for key in ("source", "revision", "version"):
        assert f"org.opencontainers.image.{key}=" in labels


def test_version_step_reads_pyproject():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    ver = next(s for s in STEPS if s.get("id") == "ver")
    assert "pyproject.toml" in ver["run"] and "['project']['version']" in ver["run"]


def test_buildx_cache_platform_and_pinned_majors():
    for s in STEPS:
        if s.get("uses", "").startswith("docker/build-push-action"):
            assert s["with"]["platforms"] == "linux/amd64"
            assert s["with"]["cache-from"] == "type=gha" and s["with"]["cache-to"] == "type=gha,mode=max"
    _step("docker/setup-buildx-action")
    for s in STEPS:
        if "uses" in s:
            assert re.fullmatch(r"[\w./-]+@v\d+", s["uses"]), s["uses"]


def test_smoke_gate_fails_closed():
    smoke = next(s for s in BUILD["steps"] if "docker-smoke.sh" in s.get("run", ""))
    assert "set -euo pipefail" in smoke["run"] and "grep -qx 'docker-smoke OK'" in smoke["run"]
    assert "continue-on-error" not in TEXT


def test_smoke_targets_the_published_image():
    """Build once; smoke that tag; publish that same image (id re-checked), never a second build."""
    builds = [s for s in STEPS if s.get("uses", "").startswith("docker/build-push-action")]
    assert len(builds) == 1 and builds[0] in BUILD["steps"]
    b = builds[0]["with"]
    assert b["load"] is True and b["tags"] == SMOKE_TAG
    smoke = next(s for s in BUILD["steps"] if "docker-smoke.sh" in s.get("run", ""))
    assert smoke["env"]["TCGWATCH_SMOKE_IMAGE"] == SMOKE_TAG  # smoke consumes the built tag, no rebuild
    # The tested image is exported with its id, loaded in publish, id compared, then the SAME tag is retagged+pushed.
    export = next(s for s in BUILD["steps"] if "docker save" in s.get("run", ""))["run"]
    assert "ci-smoke" in export and "image-id.txt" in export
    load = next(s for s in PUBLISH["steps"] if "docker load" in s.get("run", ""))["run"]
    assert 'test "$got" = "$want"' in load and "ci-smoke" in load
    push = next(s for s in PUBLISH["steps"] if "docker push" in s.get("run", ""))["run"]
    assert 'docker tag "${IMAGE_NAME}:ci-smoke" "$tag"' in push
    names = [s.get("name") for s in PUBLISH["steps"]]
    assert names.index("Load the tested image and verify its identity") < names.index("Push the tested image to GHCR")


def test_smoke_script_image_option_is_additive():
    sh = (ROOT / "scripts/docker-smoke.sh").read_text()
    assert "TCGWATCH_SMOKE_IMAGE" in sh and "pull_policy: never" in sh and "up -d --no-build" in sh
    # default path (option unset) still builds tcgwatch:local from the compose file
    assert '"${dc[@]}" build' in sh
