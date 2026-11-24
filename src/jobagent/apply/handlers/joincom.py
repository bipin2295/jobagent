"""join.com apply handler.

join.com hosts postings at ``join.com/companies/{companySlug}/{jobId}-{slug}``
and serves the application wizard at ``…/apply`` (``/application`` 404s).

The form is a Chakra UI multi-step wizard. Step 1 collects personal details
(first name, last name, email, phone) plus a resume upload; later steps add a
cover letter and salary expectation, then a submit button. Steps are advanced
with a "Next"/"Continue" control, so the handler fills what it can on each step
and advances until it reaches the submit control.

Like the other handlers it never raises: every failure is returned as an
:class:`ApplyResult` with a screenshot, and it refuses to submit when a required
field cannot be answered truthfully.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
import traceback
from pathlib import Path

import structlog

from jobagent.apply.base import ApplyHandlerBase, ApplyResult
from jobagent.apply.browser import new_page
from jobagent.apply.field_mapper import build_field_values, fill_all_common_fields
from jobagent.apply.form_utils import (
    click_robust,
    confirm_via_email,
    dismiss_consent,
    form_country_mismatch,
    submitted_at_now,
    upload_resume,
    validation_errors,
    visible_captcha,
)
from jobagent.apply.guardrails import human_like_pause
from jobagent.apply.question_engine import answer_custom_questions
from jobagent.apply.verifier import verify_form
from jobagent.db.models import Application, Job
from jobagent.profile.loader import load_profile

log = structlog.get_logger(__name__)

_MAX_STEPS = 6

#: Controls that advance the wizard to the next step.
_NEXT_SELECTORS: tuple[str, ...] = (
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Weiter')",
    "button:has-text('Suivant')",
    "button[type='submit']",
)


def _company(job: Job) -> str:
    """Best-effort company name, used to match the confirmation email."""
    if job.company_id:
        try:
            from jobagent.db.engine import get_session  # noqa: PLC0415
            from jobagent.db.models import Company  # noqa: PLC0415

            with get_session() as session:
                company = session.get(Company, job.company_id)
            if company and company.name:
                return company.name
        except Exception:
            pass
    try:
        from urllib.parse import urlparse  # noqa: PLC0415

        parts = [p for p in urlparse(job.apply_url or job.url).path.split("/") if p]
        if "companies" in parts:
            idx = parts.index("companies")
            if len(parts) > idx + 1:
                return parts[idx + 1].replace("-", " ")
    except Exception:
        pass
    return ""


async def _apply_url(job: Job) -> str:
    """Return the wizard URL for *job* (…/apply), not the listing page."""
    base = (job.apply_url or job.url or "").rstrip("/")
    if base.endswith("/apply"):
        return base
    return f"{base}/apply"


async def _is_confirmation(page) -> bool:  # type: ignore[no-untyped-def]
    url = page.url.lower()
    if any(x in url for x in ("success", "confirm", "thank", "submitted")):
        return True
    try:
        text = (await page.inner_text("body"))[:800].lower()
    except Exception:
        return False
    return any(
        x in text
        for x in (
            "thank you",
            "application received",
            "application submitted",
            "we have received",
            "successfully applied",
        )
    )


async def _click_next(page) -> bool:  # type: ignore[no-untyped-def]
    """Advance the wizard. Returns False when no advance control exists."""
    for sel in _NEXT_SELECTORS:
        loc = page.locator(sel)
        for i in range(await loc.count()):
            btn = loc.nth(i)
            try:
                if not await btn.is_visible():
                    continue
                text = (await btn.inner_text() or "").strip().lower()
                # Never advance with a button that submits the application.
                if "submit" in text:
                    continue
                await click_robust(btn)
                await asyncio.sleep(1.5)
                return True
            except Exception:
                continue
    return False


class JoinComHandler(ApplyHandlerBase):
    """Apply handler for join.com-hosted postings."""

    name = "joincom"
    ats_type = "joincom"

    async def apply(  # noqa: PLR0912, PLR0915
        self,
        job: Job,
        app: Application,
        package_dir: Path,
        shadow_mode: bool,
        manual_mode: bool = False,
    ) -> ApplyResult:
        shot_dir = package_dir / "screenshots"
        shot_dir.mkdir(exist_ok=True)

        answers_path = package_dir / "answers.json"
        try:
            answers: dict = (
                json.loads(answers_path.read_text(encoding="utf-8"))
                if answers_path.exists()
                else {}
            )
        except Exception:
            answers = {}

        resume_path = Path(app.resume_path) if app.resume_path else None

        try:
            profile = load_profile()
        except Exception as exc:
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=f"profile load failed: {exc}",
            )

        target = await _apply_url(job)

        try:
            async with new_page("joincom") as page:
                log.info("joincom.navigate", job_id=job.id, url=target[:100])
                await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
                await human_like_pause()
                await dismiss_consent(page)
                await page.screenshot(path=str(shot_dir / "step_01_loaded.png"))

                if await visible_captcha(page):
                    await page.screenshot(path=str(shot_dir / "step_captcha.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="blocked",
                        error="CAPTCHA detected before form fill",
                        screenshot_before=shot_dir / "step_captcha.png",
                    )

                field_values = build_field_values(profile, job, app.answers_json or "{}")

                # ── Step 1: email gate ───────────────────────────────────────
                # join.com opens with an email-only gate ("Your email address" +
                # Continue, or Continue with Google) before the wizard proper.
                # Without clearing this, every later step operates on a form that
                # only has that one field.
                email_val = field_values.get("email", "")
                gate = page.locator(
                    "input[placeholder*='email' i], input[type='email']"
                )
                if email_val and await gate.count() > 0:
                    only_email_visible = True
                    for probe in (
                        "input[placeholder*='first name' i]",
                        "input[placeholder*='last name' i]",
                        "input[placeholder*='phone' i]",
                    ):
                        if await page.locator(probe).count() > 0:
                            only_email_visible = False
                            break
                    if only_email_visible:
                        try:
                            await gate.first.fill("", timeout=3000)
                            await gate.first.type(email_val, delay=40, timeout=8000)
                            log.info("joincom.email_gate_filled", job_id=job.id)
                        except Exception as exc:
                            log.debug("joincom.email_gate_error", error=str(exc)[:80])
                        cont = page.locator("button:visible:has-text('Continue')")
                        if await cont.count() > 0:
                            await click_robust(cont.first)
                            await asyncio.sleep(3)
                        await page.screenshot(
                            path=str(shot_dir / "step_01b_after_email.png")
                        )

                        # join.com then requires email-link sign-in: "Check your
                        # email / We've sent you a secure login link". Read the
                        # link from Gmail and follow it to reach the wizard.
                        # The window must start AFTER this click: an older link
                        # (even minutes old) is already consumed and returns
                        # "Login link expired", which silently strands the run.
                        sent_at = datetime.now(UTC)
                        try:
                            from jobagent.email_provider import EmailProvider  # noqa: PLC0415

                            provider = EmailProvider()
                            if provider.connected:
                                res = await provider.wait_for_verification_email(
                                    alias=email_val,
                                    since=sent_at,
                                    timeout_seconds=150,
                                    subject_hint="sign in",
                                )
                                if res.found and res.link:
                                    log.info(
                                        "joincom.login_link_followed",
                                        job_id=job.id,
                                    )
                                    await page.goto(
                                        res.link,
                                        wait_until="domcontentloaded",
                                        timeout=30_000,
                                    )
                                    await asyncio.sleep(4)
                                    await page.screenshot(
                                        path=str(shot_dir / "step_01c_after_login.png")
                                    )
                                    body = (await page.inner_text("body"))[:400].lower()
                                    if "expired" in body:
                                        log.warning(
                                            "joincom.login_link_expired", job_id=job.id
                                        )
                                else:
                                    log.warning(
                                        "joincom.login_link_missing", job_id=job.id
                                    )
                            else:
                                log.warning("joincom.gmail_unavailable", job_id=job.id)
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "joincom.login_link_error", error=str(exc)[:120]
                            )

                # ── Walk the wizard, filling each step ───────────────────────
                filled_steps = 0
                for step in range(_MAX_STEPS):
                    await fill_all_common_fields(page, field_values, resume_path)

                    if resume_path is not None and resume_path.exists():
                        # Only re-upload when nothing is attached yet.
                        from jobagent.apply.form_utils import resume_attached  # noqa: PLC0415

                        if not await resume_attached(page, resume_path.name):
                            await upload_resume(page, page, resume_path)

                    # Cover letter (text area or upload) if the step offers one.
                    cl_path = package_dir / "cover_letter.txt"
                    if cl_path.exists():
                        cl_area = page.locator(
                            "textarea[placeholder*='cover' i], "
                            "textarea[name*='cover' i], "
                            "textarea[aria-label*='cover' i]"
                        )
                        if await cl_area.count() > 0:
                            try:
                                await cl_area.first.fill(
                                    cl_path.read_text(encoding="utf-8")
                                )
                            except Exception:
                                pass

                    await page.screenshot(
                        path=str(shot_dir / f"step_02_filled_{step:02d}.png")
                    )

                    if not await _click_next(page):
                        break
                    filled_steps += 1
                    await human_like_pause()

                await page.screenshot(path=str(shot_dir / "step_03_before_submit.png"))

                # ── Country guard ────────────────────────────────────────────
                mismatch = await form_country_mismatch(page, job)
                if mismatch:
                    await page.screenshot(path=str(shot_dir / "step_country.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            f"Form asks for work authorization in {mismatch}, "
                            f"but the job country is {job.country!r}."
                        ),
                        screenshot_before=shot_dir / "step_country.png",
                    )

                # ── Custom questions ─────────────────────────────────────────
                unresolved = await answer_custom_questions(page, page, job, profile)
                if unresolved:
                    await page.screenshot(path=str(shot_dir / "step_unanswerable.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            "Unanswerable required question(s): "
                            + "; ".join(unresolved[:6])
                        ),
                        screenshot_before=shot_dir / "step_unanswerable.png",
                    )

                verification = await verify_form(
                    page, app, profile, shot_dir / "step_04_verify.png", scope=page
                )
                if not verification.passed:
                    await page.screenshot(
                        path=str(shot_dir / "step_04_verify_failed.png")
                    )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            "Pre-submit verifier failed: "
                            + "; ".join(verification.issues[:6])
                        ),
                        screenshot_before=shot_dir / "step_04_verify_failed.png",
                    )

                # ── Shadow / manual gates ────────────────────────────────────
                if shadow_mode:
                    log.info("joincom.shadow_stopped", job_id=job.id, steps=filled_steps)
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="shadow_stopped",
                        screenshot_before=shot_dir / "step_03_before_submit.png",
                    )

                if manual_mode:
                    from jobagent.apply.manual import (  # noqa: PLC0415
                        await_manual_submit,
                        focus_form,
                    )

                    await focus_form(page)
                    decision = await await_manual_submit(
                        company=_company(job),
                        title=job.title or "(untitled role)",
                        url=page.url,
                    )
                    await page.screenshot(path=str(shot_dir / "step_05_manual.png"))
                    if decision == "submitted":
                        return ApplyResult(
                            success=True,
                            application_id=app.id,
                            status="submitted",
                            confirmation_url=page.url,
                            confirmation_text="Submitted manually by operator",
                            screenshot_before=shot_dir / "step_03_before_submit.png",
                            screenshot_after=shot_dir / "step_05_manual.png",
                        )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="skipped",
                        error=f"Manual hand-off: operator chose {decision!r}",
                        extra={"quit": decision == "quit"},
                    )

                # ── Submit ───────────────────────────────────────────────────
                submit = page.locator(
                    "button:visible:has-text('Submit'), "
                    "button[type='submit']:visible"
                )
                if await submit.count() == 0:
                    await page.screenshot(path=str(shot_dir / "step_no_submit.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            "Submit button not found after "
                            f"{filled_steps} wizard step(s). The wizard may need a "
                            "step this handler does not handle."
                        ),
                        screenshot_before=shot_dir / "step_no_submit.png",
                    )

                submitted_at = submitted_at_now()
                log.info("joincom.submit_clicked", job_id=job.id, steps=filled_steps)
                await submit.first.click(timeout=15_000)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                except Exception:
                    pass
                await asyncio.sleep(5)
                await page.screenshot(path=str(shot_dir / "step_06_after_submit.png"))

                if await visible_captcha(page):
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="blocked",
                        error="CAPTCHA appeared after submit",
                        screenshot_before=shot_dir / "step_03_before_submit.png",
                        screenshot_after=shot_dir / "step_06_after_submit.png",
                    )

                confirmed = await _is_confirmation(page)
                if not confirmed:
                    confirmed = await confirm_via_email(_company(job), submitted_at)

                if confirmed:
                    log.info("joincom.submitted", job_id=job.id, url=page.url)
                    return ApplyResult(
                        success=True,
                        application_id=app.id,
                        status="submitted",
                        confirmation_url=page.url,
                        confirmation_text="Application submitted successfully via join.com",
                        screenshot_before=shot_dir / "step_03_before_submit.png",
                        screenshot_after=shot_dir / "step_06_after_submit.png",
                    )

                validation = await validation_errors(page)
                if validation:
                    log.warning("joincom.submit_blocked", job_id=job.id, errors=validation)
                return ApplyResult(
                    success=False,
                    application_id=app.id,
                    status="failed",
                    error=(
                        "Confirmation not detected after submit"
                        + (f" | validation: {'; '.join(validation)}" if validation else "")
                    ),
                    screenshot_before=shot_dir / "step_03_before_submit.png",
                    screenshot_after=shot_dir / "step_06_after_submit.png",
                )

        except Exception as exc:  # noqa: BLE001
            log.exception("joincom.apply_error", job_id=job.id, error=str(exc))
            before = shot_dir / "step_03_before_submit.png"
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                screenshot_before=before if before.exists() else None,
            )
