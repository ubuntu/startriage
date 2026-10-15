"""Tests for the skill resources and the host checks on submissions."""

from __future__ import annotations

import pytest

from startriage.ai import checks
from startriage.ai.checks import URLChecker, check_bug_ref, check_series
from startriage.ai.skills import CORE, PHASES, load_package_notes, load_skill, system_prompt
from startriage.ai.tools import REGISTRY, ToolError
from startriage.ai.tools.http import NetworkError

# --- skills -----------------------------------------------------------------


@pytest.mark.parametrize("phase", PHASES)
def test_phase_skills_load_with_known_tools(phase):
    skill = load_skill(phase)
    assert skill.name == phase
    assert skill.description
    assert skill.submit.startswith("submit_")
    assert set(skill.tools) <= set(REGISTRY)
    assert not skill.body.startswith("---")
    assert len(skill.sha256) == 64


def test_package_notes_sections_are_phases():
    for package in ("mysql", "libvirt"):
        notes = load_package_notes(package)
        assert notes is not None
        assert set(notes.sections) <= set(PHASES)
    assert load_package_notes("no-such-package") is None


def test_system_prompt_adds_package_notes_only_to_their_phase():
    validate = system_prompt("validate-report", ["libvirt", "libvirt"])
    assert validate.startswith(load_skill(CORE).body)
    assert validate.count("## Notes for libvirt") == 1
    assert "## Notes for libvirt" not in system_prompt("write-report", ["libvirt"])


# --- checks -----------------------------------------------------------------


def test_check_series():
    table = {"noble": {}, "resolute": {}}
    assert check_series(["noble"], table) == []
    assert check_series(["warty"], table) == [
        "series 'warty' is not an active Ubuntu series (use one of: noble, resolute)"
    ]
    assert check_series(["warty"], {}) == []


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("", None),
        ("#42", None),
        ("100", "cannot be the bug itself"),
        ("LP42", "must be a Launchpad bug number"),
    ],
)
def test_check_bug_ref(value, problem):
    problems = check_bug_ref(value, "100")
    if problem is None:
        assert problems == []
    else:
        assert problem in problems[0]


@pytest.mark.asyncio
async def test_url_checker(monkeypatch, caplog):
    calls: list[str] = []

    async def fake_status(http, url):
        calls.append(url)
        if "missing" in url:
            return 404, url
        if "private" in url:
            raise ToolError("non-public address")
        if "down" in url:
            raise NetworkError("HEAD failed: TimeoutError")
        if "weird" in url:
            raise OSError("boom")
        return 200, url

    monkeypatch.setattr(checks, "url_status", fake_status)
    checker = URLChecker(http=object())  # ty: ignore[invalid-argument-type]
    urls = [
        "https://ok.test",
        "https://missing.test",
        "https://private.test",
        "https://down.test",
        "https://weird.test",
    ]
    problems = await checker.check([*urls, "https://ok.test"])
    assert len(problems) == 2
    assert "HTTP 404" in problems[0]
    assert "non-public" in problems[1]
    assert "Could not verify https://down.test" in caplog.text
    await checker.check(urls)
    assert calls == urls  # cached per run
    assert await URLChecker(None).check(urls) == []
