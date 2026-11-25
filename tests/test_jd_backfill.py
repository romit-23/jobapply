"""Unit tests for ``jobapply.jd_backfill``.

We never hit the real network — every test injects an ``httpx.Client``
backed by a ``MockTransport`` so we can drive both happy paths and
the auth-wall / failure paths deterministically.
"""

from __future__ import annotations

import httpx
import pytest

from jobapply.jd_backfill import (
    JdBackfillError,
    _normalize_linkedin_url,
    fetch_description_from_url,
)


def _make_client(handler: callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_normalize_linkedin_url_extracts_numeric_id() -> None:
    """LinkedIn search results sometimes give us slug-y URLs; the
    canonical ``/jobs/view/<numeric>`` form is what JobSpy hits and
    is the most likely to return the JD HTML."""
    assert (
        _normalize_linkedin_url("https://www.linkedin.com/jobs/view/etl-sql-at-pwc-4408659493")
        == "https://www.linkedin.com/jobs/view/4408659493"
    )
    assert (
        _normalize_linkedin_url("https://www.linkedin.com/jobs/view/4408659493")
        == "https://www.linkedin.com/jobs/view/4408659493"
    )


def test_normalize_linkedin_url_passthrough_for_other_hosts() -> None:
    url = "https://www.indeed.com/viewjob?jk=abc123"
    assert _normalize_linkedin_url(url) == url


def test_fetch_description_from_url_rejects_non_http() -> None:
    with pytest.raises(JdBackfillError):
        fetch_description_from_url("file:///etc/passwd")


def test_fetch_description_from_url_returns_none_for_empty() -> None:
    assert fetch_description_from_url("") is None
    assert fetch_description_from_url("   ") is None


def test_fetch_description_from_url_extracts_linkedin_markup() -> None:
    """Mirror JobSpy: the JD lives in
    ``div.show-more-less-html__markup`` on the public LinkedIn job
    page. We must extract that block specifically (not the
    nav/footer)."""
    html = (
        "<html><body>"
        "<nav>Sign in</nav>"
        "<div class='show-more-less-html__markup'>"
        + ("Senior backend engineer responsibilities. " * 20)
        + "</div>"
        "<footer>About</footer>"
        "</body></html>"
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        # Confirm the LinkedIn-specific UA + Accept headers got through.
        assert (
            "linkedin.com" in request.headers["user-agent"].lower()
            or "Chrome" in request.headers["user-agent"]
        )
        return httpx.Response(200, text=html)

    with _make_client(_handler) as c:
        out = fetch_description_from_url("https://www.linkedin.com/jobs/view/4408659493", client=c)
    assert out is not None
    assert "Senior backend engineer" in out
    assert "Sign in" not in out
    assert "About" not in out


def test_fetch_description_from_url_returns_none_on_authwall() -> None:
    """LinkedIn's anti-bot redirects guest fetches to /signup or
    /authwall — those landing pages are useless to us, so the
    backfill should signal failure rather than returning the auth
    HTML as the JD."""

    def _redirecting(request: httpx.Request) -> httpx.Response:
        if "/jobs/view/" in str(request.url):
            return httpx.Response(
                302,
                headers={"location": "https://www.linkedin.com/signup"},
            )
        # /signup landing page — long enough to look like real content.
        return httpx.Response(
            200,
            text="<html><body>" + ("Sign in to LinkedIn. " * 50) + "</body></html>",
        )

    with _make_client(_redirecting) as c:
        out = fetch_description_from_url("https://www.linkedin.com/jobs/view/4408659493", client=c)
    assert out is None


def test_fetch_description_from_url_returns_none_on_4xx() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with _make_client(_handler) as c:
        assert (
            fetch_description_from_url("https://www.indeed.com/viewjob?jk=missing", client=c)
            is None
        )


def test_fetch_description_from_url_returns_none_on_network_error() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with _make_client(_handler) as c:
        assert fetch_description_from_url("https://www.linkedin.com/jobs/view/1", client=c) is None


def test_fetch_description_from_url_uses_indeed_selector() -> None:
    """Indeed JDs live in ``#jobDescriptionText``; make sure the
    board-specific selector takes precedence over the generic
    fallbacks (which would otherwise grab the surrounding article)."""
    html = (
        "<html><body>"
        "<article>Whole page navigation and sidebar.</article>"
        "<div id='jobDescriptionText'>"
        + ("Looking for a data engineer with strong SQL skills. " * 10)
        + "</div>"
        "</body></html>"
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    with _make_client(_handler) as c:
        out = fetch_description_from_url("https://www.indeed.com/viewjob?jk=abc", client=c)
    assert out is not None
    assert "data engineer with strong SQL" in out


def test_fetch_description_from_url_falls_back_to_article() -> None:
    """For unknown sites we should still try generic selectors before
    giving up — the long ``<article>`` body is what callers care about."""
    html = (
        "<html><body>"
        "<header>Site nav</header>"
        "<article>"
        + ("Sample JD body for the new generic-host extractor path. " * 10)
        + "</article>"
        "</body></html>"
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    with _make_client(_handler) as c:
        out = fetch_description_from_url("https://careers.example.com/job/123", client=c)
    assert out is not None
    assert "generic-host extractor" in out


def test_fetch_description_returns_none_when_text_too_short() -> None:
    """A handful of words isn't a JD — typically a 'Job not found'
    placeholder. We'd rather report the failure than tailor a resume
    against three lines of nav text."""
    html = "<html><body><article>Tiny placeholder.</article></body></html>"

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    with _make_client(_handler) as c:
        assert fetch_description_from_url("https://careers.example.com/job/1", client=c) is None
