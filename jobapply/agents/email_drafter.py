"""Email-drafting agent for the ``jobapply tailor`` flow.

Given the tailored resume + cover letter the user is about to send, this
agent produces a short, ready-to-paste application email — subject line
plus body — addressed to a specific recruiter or hiring-manager email
address. Optional ``additional_info`` from the user (e.g. "I met you at
PyCon", "referred by Jane", "available to start in two weeks") is folded
into the prompt so the draft sounds personal rather than generic.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from jobapply.agents._structured import invoke_structured
from jobapply.models import CoverLetter, EmailDraft, RawJob, TailoredResume


def draft_application_email(
    llm: BaseChatModel,
    *,
    profile_text: str,
    job: RawJob,
    resume: TailoredResume | None = None,
    cover: CoverLetter | None = None,
    recipient_email: str,
    additional_info: str = "",
    sender_name: str = "",
) -> EmailDraft:
    """Produce an :class:`EmailDraft` ready to copy into a mail client.

    ``resume`` and ``cover`` are optional. When both are present the
    email summarizes the tailored cover letter and mentions the
    attached artifacts — that's the polished "I just ran the tailor
    pipeline" flow. When either is missing (e.g. the user clicks
    "Draft email" on a freshly-searched, not-yet-tailored job) we
    fall back to drafting from the JD + profile alone and instruct
    the model not to claim attachments it can't actually deliver.
    ``additional_info`` is optional: when blank we just rely on the
    JD + resume / profile context.
    """
    fallback_name = ((resume.document_title if resume else "") or "").strip()
    name = (sender_name or fallback_name).strip() or "the candidate"
    additional = (additional_info or "").strip() or "(none provided)"

    has_artifacts = resume is not None and cover is not None
    attachment_clause = (
        "Mention that the tailored resume and cover letter are attached. "
        if has_artifacts
        else (
            "Do NOT claim that a resume or cover letter is attached — "
            "this email is being sent ahead of any attachments. "
            "Instead, offer to send a tailored resume on request. "
        )
    )
    grounding_clause = (
        "two or three concrete reasons you're a strong fit (pulled "
        "from the resume / cover letter)"
        if has_artifacts
        else (
            "two or three concrete reasons you're a strong fit "
            "(pulled from the candidate's profile and the JD)"
        )
    )

    sys = SystemMessage(
        content=(
            "You draft a concise, ready-to-paste job application email. "
            "Output strictly two fields:\n"
            "- `subject`: a single specific line, no quotes or trailing "
            "punctuation. Mention the role and (when natural) the "
            "candidate's name.\n"
            f"- `body`: 3-5 short paragraphs of plain text. Greet the "
            f"recipient (use 'Hi <Name>,' if a name is obvious from the "
            f"recipient address; otherwise 'Hello,'). State the role "
            f"you're applying for, {grounding_clause}, and "
            f"close with a clear call to action. {attachment_clause}"
            f"Sign off with the candidate's real name.\n\n"
            "RULES: stay truthful — never invent employers, metrics, "
            "dates, or projects beyond what the resume / profile "
            "supports. Avoid clichés ('passionate', 'rockstar', "
            "'synergy') and any phrasing that sounds AI-generated. "
            "Keep the body under 180 words."
        ),
    )

    artifact_block = ""
    if has_artifacts:
        # Type narrowing: the has_artifacts guard guarantees both are set.
        assert resume is not None and cover is not None
        artifact_block = (
            f"COVER LETTER (source of voice + facts):\n"
            f"opening: {cover.opening}\n"
            f"body: {cover.body}\n"
            f"closing: {cover.closing}\n\n"
            f"TAILORED RESUME SUMMARY: {resume.summary}\n"
            f"TOP SKILLS: {', '.join(resume.skills[:20])}\n\n"
        )
    else:
        artifact_block = (
            "NOTE: no tailored resume / cover letter is available yet — "
            "ground the email entirely in the profile excerpt and JD "
            "below. Do not fabricate attachments.\n\n"
        )

    jd_excerpt = (job.description or "").strip()[:4000]
    user = HumanMessage(
        content=(
            f"RECIPIENT EMAIL: {recipient_email}\n"
            f"CANDIDATE NAME: {name}\n"
            f"ROLE: {job.title or '(unknown)'} at "
            f"{job.company or '(unknown)'}\n"
            f"JOB URL: {job.job_url or job.apply_url or '(none)'}\n\n"
            f"USER-PROVIDED CONTEXT (weave in naturally if helpful):\n"
            f"{additional}\n\n"
            f"{artifact_block}"
            f"JOB DESCRIPTION EXCERPT:\n{jd_excerpt or '(empty)'}\n\n"
            f"PROFILE EXCERPT (for additional grounding):\n"
            f"{(profile_text or '')[:4000]}"
        ),
    )
    result = invoke_structured(llm, EmailDraft, [sys, user])

    if not result.to.strip() and recipient_email.strip():
        result.to = recipient_email.strip()
    return result
