"""M03 skill and prompt registry acceptance (deterministic fixtures, no model calls)."""

import hashlib
import json
import shutil

import pytest

from ghostlab.skills import SkillError, SkillPermissionError, SkillRegistry
from ghostlab.skills.registry import ROOT

from m03_fakes import scope

INITIAL = {"propose-refund-scenario", "interpret-evidence", "suggest-idempotency-repair"}


@pytest.fixture
def copied(tmp_path):
    shutil.copytree(ROOT / "skills", tmp_path / "skills")
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    return tmp_path


def load(root):
    return SkillRegistry.load(root / "skills", root / "prompts")


def test_initial_procedures_load_and_validate():
    registry = SkillRegistry.load()
    assert {s.spec.skill_id for s in registry.all()} == INITIAL
    for skill in registry.all():
        raw = (ROOT / skill.source).read_bytes().replace(b"\r\n", b"\n")
        assert skill.sha256 == hashlib.sha256(raw).hexdigest()
        assert skill.spec.allowed_tools == []  # model roles propose; they never call tools
        registry.prompts.get(skill.spec.prompt_version)


def test_output_contract_publication_is_reported():
    registry = SkillRegistry.load()
    published = {s.spec.skill_id: s.output_contract_registered for s in registry.all()}
    assert published == {"propose-refund-scenario": True, "interpret-evidence": False,
                         "suggest-idempotency-repair": False}


def test_skill_cannot_grant_tools_beyond_scope(copied):
    path = copied / "skills" / "propose-refund-scenario.v1.json"
    spec = json.loads(path.read_text())
    spec["allowed_tools"] = ["issue_refund"]
    path.write_text(json.dumps(spec))
    registry = load(copied)
    skill = registry.get("propose-refund-scenario")
    narrow = scope()
    with pytest.raises(SkillPermissionError, match="issue_refund"):
        registry.authorize(skill, narrow)
    assert narrow.allowed_tools == []  # scope untouched
    wide = scope(tools=["issue_refund", "get_order"])
    assert registry.authorize(skill, wide).skill_id == "propose-refund-scenario"


def test_invocation_records_skill_and_prompt_versions():
    registry = SkillRegistry.load()
    skill = registry.get("propose-refund-scenario")
    invocation = registry.authorize(skill, scope())
    prompt = registry.prompts.get("explorer-prompt-1")
    assert (invocation.skill_id, invocation.skill_version, invocation.skill_sha256) == (
        "propose-refund-scenario", 1, skill.sha256)
    assert (invocation.prompt_version, invocation.prompt_sha256) == (
        "explorer-prompt-1", prompt.sha256)


def test_changed_procedure_or_prompt_changes_digests(copied):
    before = load(copied)
    skill_path = copied / "skills" / "propose-refund-scenario.v1.json"
    spec = json.loads(skill_path.read_text())
    spec["steps"][0] += " carefully"
    skill_path.write_text(json.dumps(spec))
    prompt_path = copied / "prompts" / "explorer-prompt-1.md"
    prompt_path.write_text(prompt_path.read_text() + "\nExtra rule.\n")
    after = load(copied)
    assert after.get("propose-refund-scenario").sha256 != before.get("propose-refund-scenario").sha256
    assert (after.prompts.get("explorer-prompt-1").sha256
            != before.prompts.get("explorer-prompt-1").sha256)


def test_line_endings_do_not_change_digests(copied):
    path = copied / "skills" / "interpret-evidence.v1.json"
    lf = load(copied).get("interpret-evidence").sha256
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert load(copied).get("interpret-evidence").sha256 == lf


def test_new_version_is_selected_by_default_and_old_stays_addressable(copied):
    old = copied / "skills" / "propose-refund-scenario.v1.json"
    spec = json.loads(old.read_text())
    spec["version"] = 2
    (copied / "skills" / "propose-refund-scenario.v2.json").write_text(json.dumps(spec))
    registry = load(copied)
    assert registry.get("propose-refund-scenario").spec.version == 2
    assert registry.get("propose-refund-scenario", 1).spec.version == 1


@pytest.mark.parametrize("mutate,match", [
    (lambda d: (d / "skills" / "x.json").write_text("{}"), "not a valid SkillSpec"),
    (lambda d: (d / "skills" / "propose-refund-scenario.v1.json").rename(
        d / "skills" / "renamed.json"), "must be named"),
    (lambda d: (d / "prompts" / "explorer-prompt-1.md").unlink(), "Unknown prompt"),
    (lambda d: (d / "prompts" / "explorer-prompt-1.md").write_text("   "), "empty"),
    (lambda d: (d / "skills" / "interpret-evidence.v1.json").write_text(
        (d / "skills" / "interpret-evidence.v1.json").read_text().replace(
            '"schema_version": 1', '"schema_version": 1, "grants_admin": true')),
     "not a valid SkillSpec"),
], ids=["invalid", "misnamed", "missing-prompt", "empty-prompt", "unknown-field"])
def test_bad_skill_files_are_refused(copied, mutate, match):
    mutate(copied)
    with pytest.raises(SkillError, match=match):
        load(copied)


def test_duplicate_skill_version_is_refused(copied):
    registry = load(copied)
    skill = registry.get("interpret-evidence")
    with pytest.raises(SkillError, match="Duplicate"):
        SkillRegistry([skill, skill], registry.prompts)
