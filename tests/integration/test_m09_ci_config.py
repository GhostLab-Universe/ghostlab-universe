"""Static checks on the CI and release files. Workflows can't be run from here.

This reads the workflow as text so no YAML library is needed. It proves what the file
says, not that GitHub accepts or runs it.
"""

import re

from tests.integration.conftest import ROOT

WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")


def test_workflow_runs_every_existing_check():
    for command in ("uv lock --check", "uv sync --locked --all-extras --group dev",
                    "uv run --frozen pytest", "ghostlab.contracts.export --check",
                    "-s -m unittest discover -s tests", "contracts:verify", "contracts:typecheck",
                    "python -m ghostlab demo", "python -m ghostlab replay",
                    "python -m ghostlab journey-demo", "python -m ghostlab journey-replay",
                    "python -m evals.run"):
        assert command in WORKFLOW, command


def test_the_suite_checks_out_the_history_its_release_tests_read():
    """Ancestry and restamping tests need real history, and a shallow clone has none."""
    job = WORKFLOW.split("\n  python:\n")[1].split("\n  demos:\n")[0]
    assert "uv run --frozen pytest" in job
    assert re.search(r"actions/checkout@\S+ # \S+\n\s+with:\n\s+fetch-depth: 0\b", job)


def test_tool_versions_are_pinned():
    assert 'version: "0.13.6"' in WORKFLOW
    assert "node-version-file: .nvmrc" in WORKFLOW
    for use in re.findall(r"uses:\s*(\S+)", WORKFLOW):
        assert re.search(r"@[0-9a-f]{40}$", use), use


def test_workflow_has_no_secrets_deployment_or_write_access():
    assert "permissions:\n  contents: read" in WORKFLOW
    assert "secrets." not in WORKFLOW and "API_KEY" not in WORKFLOW
    assert "LANGSMITH_TRACING: \"false\"" in WORKFLOW
    for word in ("deploy", "gh pr merge", "git push", "--write-expected"):
        assert word not in WORKFLOW.lower(), word


def test_every_check_feeds_the_promotion_gate():
    jobs = re.findall(r"^  ([a-z-]+):$", WORKFLOW.split("\njobs:\n")[1], flags=re.M)
    needed = re.search(r"promotion-gate:.*?needs: \[(.*?)\]", WORKFLOW, flags=re.S).group(1)
    assert {name.strip() for name in needed.split(",")} == set(jobs) - {"promotion-gate"}


def test_deploy_documents_exist_and_name_their_targets():
    checklist = (ROOT / "deploy" / "release-checklist.md").read_text("utf-8")
    drill = (ROOT / "deploy" / "recovery-drill.md").read_text("utf-8")
    assert "release-targets.json" in checklist and "ops.recovery_drill" in drill
