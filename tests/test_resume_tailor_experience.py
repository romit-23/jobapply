"""Resume-tailor must derive experience + projects deterministically from
the profile and only let the LLM rewrite bullets.

These tests pin three guarantees that matter in production:

* Employer / role / date metadata always comes from the profile —
  never the LLM — so we cannot fabricate or reformat work history.
* When the LLM does provide rewritten bullets per role, they are
  paired by index to the source profile order.
* When the LLM omits a role's bullets (smaller / free-tier models
  often return shorter lists than asked), the corresponding row
  falls back to the profile's verbatim bullets so the candidate's
  history is never lost.
"""

from __future__ import annotations

import inspect
from typing import Any

from jobapply.agents import resume_tailor as rt_module
from jobapply.agents.resume_tailor import (
    _experience_from_profile,
    _pair_experience,
    _pair_projects,
    _projects_from_profile,
    _TailoredBullets,
    tailor_resume,
)
from jobapply.models import RawJob
from jobapply.profile import (
    Profile,
    ProfileEducation,
    ProfileExperience,
    ProfileProject,
)

_PROFILE_TEXT = (
    "# Profile\n"
    "## Header\n- **Name:** Sakshi B.\n- **Email:** s@example.com\n\n"
    "## Skills\n- ETL\n- SQL\n\n"
    "## Experience\n### Fintellix | Consultant I | Mar 2025 - Present\n"
    "- Led ADF implementation for RBI compliance\n"
    "## Education\n### Terna College | BE | 2018 - 2022\n"
)


def _profile_with_experience() -> Profile:
    return Profile(
        name="Sakshi B.",
        email="s@example.com",
        phone="555-0100",
        location="Mumbai, India",
        linkedin="https://linkedin.com/in/sakshi",
        skills=["ETL", "SQL"],
        experience=[
            ProfileExperience(
                company="Fintellix",
                role="Consultant I",
                start_date="March 2025",
                end_date="Present",
                bullets=[
                    "Led ADF implementation for RBI compliance.",
                    "Architected ETL integrations using Informatica.",
                ],
            ),
            ProfileExperience(
                company="Fintellix",
                role="Associate Consultant",
                start_date="July 2022",
                end_date="March 2025",
                bullets=[
                    "Designed complex ETL workflows.",
                    "Built data mappings for regulatory reporting.",
                ],
            ),
        ],
        projects=[
            ProfileProject(
                name="CIMS Reporting",
                description="Daily RBI submissions.",
                bullets=["Reduced manual ops by 30%.", "BRFSD-compliant pipelines."],
            ),
        ],
        education=[
            ProfileEducation(
                school="Terna College",
                degree="BE",
                location="Mumbai, India",
                start_date="2018",
                end_date="2022",
                gpa="8.1",
            ),
        ],
    )


# Test fixtures ------------------------------------------------------------ #


class _FakeStructured:
    """Mimics the object ``llm.with_structured_output`` returns."""

    def __init__(self, out: Any) -> None:
        self._out = out

    def invoke(self, _msgs: Any) -> Any:
        return self._out


class _BulletsLLM:
    """Returns whatever ``_TailoredBullets`` payload the test sets up.

    Only handles the new ``_TailoredBullets`` schema — production
    callers always pass a structured profile, so the legacy
    full-TailoredResume path isn't exercised here.
    """

    def __init__(self, payload: _TailoredBullets) -> None:
        self._payload = payload

    def with_structured_output(self, schema: type[Any]) -> _FakeStructured:
        assert schema is _TailoredBullets, (
            "When a structured Profile is passed, the agent must ask the "
            "LLM for the lightweight bullet schema, not a full TailoredResume."
        )
        return _FakeStructured(self._payload)


def _job() -> RawJob:
    return RawJob(
        job_id="job1",
        title="ETL Developer",
        company="PwC",
        description="Looking for ETL + SQL skills.",
    )


# Prompt expectations ----------------------------------------------------- #


def test_system_prompt_explicitly_keeps_metadata_out_of_llm() -> None:
    """The new prompt must tell the model NOT to emit contact /
    education / dates etc. — those are filled deterministically and
    should never round-trip through the LLM."""
    src = inspect.getsource(rt_module._request_tailored_bullets)
    src_lower = src.lower()
    assert "deterministically" in src_lower
    assert "do not include" in src_lower or "do not include any" in src_lower
    # Schema fields the LLM is allowed to emit are still called out.
    assert "experience_bullets" in src
    assert "project_bullets" in src
    assert "skills" in src


# Determinism: contact / education / dates always from profile ----------- #


def test_contact_always_comes_from_profile_not_llm() -> None:
    """Even if the LLM sends weird skills, the contact block on the
    rendered resume always reflects the profile verbatim."""
    payload = _TailoredBullets(
        summary="Engineer.",
        skills=["ETL", "SQL"],
        experience_bullets=[["new bullet 1"], ["new bullet 2"]],
        project_bullets=[["new project bullet"]],
    )
    out = tailor_resume(
        _BulletsLLM(payload),
        profile_text=_PROFILE_TEXT,
        job=_job(),
        skills=[],
        profile_skills=["ETL", "SQL"],
        profile=_profile_with_experience(),
    )
    assert out.document_title == "Sakshi B."
    assert out.contact.email == "s@example.com"
    assert out.contact.phone == "555-0100"
    assert out.contact.location == "Mumbai, India"
    assert out.contact.linkedin == "https://linkedin.com/in/sakshi"
    # The plain-text fallback line includes the same data joined by " | ".
    assert "s@example.com" in out.contact_line
    assert "555-0100" in out.contact_line
    assert "Mumbai, India" in out.contact_line


def test_education_metadata_always_comes_from_profile() -> None:
    payload = _TailoredBullets(
        summary="…",
        skills=["ETL"],
        experience_bullets=[[], []],
        project_bullets=[[]],
    )
    out = tailor_resume(
        _BulletsLLM(payload),
        profile_text=_PROFILE_TEXT,
        job=_job(),
        skills=[],
        profile_skills=["ETL"],
        profile=_profile_with_experience(),
    )
    assert len(out.education) == 1
    ed = out.education[0]
    assert ed.school == "Terna College"
    assert ed.degree == "BE"
    assert ed.location == "Mumbai, India"
    assert ed.gpa == "8.1"
    assert ed.dates == "2018 – 2022"


def test_experience_metadata_always_comes_from_profile() -> None:
    """Even when the LLM rewrites bullets, company / role / dates are
    copied verbatim from the profile so the model can't fabricate
    employers or massage dates."""
    payload = _TailoredBullets(
        summary="…",
        skills=["ETL", "SQL"],
        experience_bullets=[
            ["Tailored bullet for role 0."],
            ["Tailored bullet for role 1."],
        ],
        project_bullets=[["Tailored project bullet."]],
    )
    out = tailor_resume(
        _BulletsLLM(payload),
        profile_text=_PROFILE_TEXT,
        job=_job(),
        skills=[],
        profile_skills=["ETL", "SQL"],
        profile=_profile_with_experience(),
    )
    assert [r.company for r in out.experience] == ["Fintellix", "Fintellix"]
    assert [r.role for r in out.experience] == ["Consultant I", "Associate Consultant"]
    # Dates come from profile.start_date/end_date, not from the LLM.
    assert out.experience[0].dates == "March 2025 – Present"
    assert out.experience[1].dates == "July 2022 – March 2025"


# LLM bullet rewrites are paired by index --------------------------------- #


def test_llm_bullets_paired_by_role_index() -> None:
    payload = _TailoredBullets(
        summary="Engineer.",
        skills=["SQL", "ETL"],
        experience_bullets=[
            ["Rewrote ADF flows for RBI."],
            ["Rewrote ETL workflows for banking."],
        ],
        project_bullets=[["Rewrote CIMS bullet."]],
    )
    out = tailor_resume(
        _BulletsLLM(payload),
        profile_text=_PROFILE_TEXT,
        job=_job(),
        skills=[],
        profile_skills=["ETL", "SQL"],
        profile=_profile_with_experience(),
    )
    assert out.experience[0].bullets == ["Rewrote ADF flows for RBI."]
    assert out.experience[1].bullets == ["Rewrote ETL workflows for banking."]
    # Project description is still promoted to the first bullet so it
    # surfaces in the rendered resume regardless of what the LLM
    # emitted.
    assert out.projects[0].bullets[0] == "Daily RBI submissions."
    assert "Rewrote CIMS bullet." in out.projects[0].bullets


def test_missing_or_empty_llm_bullets_fall_back_to_profile() -> None:
    """LLM returns bullets for role 0 only — role 1 must still appear,
    populated from the profile's source bullets."""
    payload = _TailoredBullets(
        summary="…",
        skills=["ETL"],
        experience_bullets=[["Rewrote bullet for role 0."]],
        project_bullets=[],
    )
    out = tailor_resume(
        _BulletsLLM(payload),
        profile_text=_PROFILE_TEXT,
        job=_job(),
        skills=[],
        profile_skills=["ETL"],
        profile=_profile_with_experience(),
    )
    assert len(out.experience) == 2
    assert out.experience[0].bullets == ["Rewrote bullet for role 0."]
    assert out.experience[1].bullets == [
        "Designed complex ETL workflows.",
        "Built data mappings for regulatory reporting.",
    ]
    # Same for projects: empty LLM list → fall back to profile bullets +
    # promoted description.
    assert len(out.projects) == 1
    assert out.projects[0].bullets[0] == "Daily RBI submissions."
    assert "Reduced manual ops by 30%." in out.projects[0].bullets


# Helper sanity checks ---------------------------------------------------- #


def test_experience_helper_skips_blank_rows() -> None:
    """A profile row with no company / role / bullets is junk and
    should be dropped from the fallback so we don't render an empty
    bullet list."""
    profile = Profile(
        experience=[
            ProfileExperience(),  # all-blank
            ProfileExperience(company="X", role="Y", bullets=["b1"]),
        ]
    )
    out = _experience_from_profile(profile)
    assert len(out) == 1
    assert out[0].company == "X"


def test_pair_experience_skips_blank_rows() -> None:
    profile = Profile(
        experience=[
            ProfileExperience(),
            ProfileExperience(company="X", role="Y", bullets=["b1"]),
        ]
    )
    out = _pair_experience(profile, [["new bullet"]])
    assert len(out) == 1
    # Blank row was skipped, so index 0 of the LLM bullets goes to the
    # SECOND profile entry.
    assert out[0].company == "X"
    assert out[0].bullets == ["new bullet"]


def test_projects_helper_promotes_description_to_bullets() -> None:
    profile = Profile(
        projects=[
            ProfileProject(name="P", description="Cool stuff", bullets=["did things"]),
        ]
    )
    out = _projects_from_profile(profile)
    assert out[0].bullets == ["Cool stuff", "did things"]


def test_pair_projects_promotes_description_even_when_llm_rewrote() -> None:
    profile = Profile(
        projects=[
            ProfileProject(name="P", description="Cool stuff", bullets=["did things"]),
        ]
    )
    out = _pair_projects(profile, [["LLM rewrote it"]])
    assert out[0].name == "P"
    assert out[0].bullets[0] == "Cool stuff"
    assert "LLM rewrote it" in out[0].bullets


def test_projects_helper_avoids_duplicate_description() -> None:
    """If the description already appears in the bullets list, don't
    add it twice — users sometimes copy the same line into both fields."""
    profile = Profile(
        projects=[
            ProfileProject(name="P", description="Cool stuff", bullets=["Cool stuff"]),
        ]
    )
    out = _projects_from_profile(profile)
    assert out[0].bullets == ["Cool stuff"]
