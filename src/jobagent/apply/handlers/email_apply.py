"""Email application handler.

Used when the job description says "send your CV to careers@company.com"
and the job's apply_url is a mailto: URI, e.g. ``mailto:careers@company.com``.

Sending is DISABLED by default via ``email.send_enabled: false`` in
``data/profile/answers_seed.yaml``.  This is an intentional safe default:
the agent will log a clear explanation and return a "skipped" result until
you explicitly opt-in by setting ``email.send_enabled: true`` in that file.

When enabled:
  - Extracts the recipient address from ``job.apply_url``.
  - Loads the cover letter from ``package_dir/cover_letter.txt``.
  - Attaches ``package_dir/resume.pdf`` (falls back to ``app.resume_path``).
  - Builds a plain-text MIME message:
      Subject : Application for <job.title> – <profile.contact.name>
      Body    : cover letter text
      Attach  : resume PDF
  - Sends via SMTP (STARTTLS) using credentials from environment variables:
        JOBAGENT_SMTP_HOST  (default: smtp.gmail.com)
        JOBAGENT_SMTP_PORT  (default: 587)
        JOBAGENT_SMTP_USER  (default: profile.contact.email)
        JOBAGENT_SMTP_PASS  (required; no default — will return "failed" if unset)

Shadow mode is respected even for email: if shadow_mode=True, the email is
NOT sent and the result status is "shadow_stopped".
"""

from __future__ import annotations

import os
import smtplib
import traceback
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import structlog
import yaml

from jobagent.apply.base import ApplyHandlerBase, ApplyResult
from jobagent.config import settings
from jobagent.db.models import Application, Job
from jobagent.profile.loader import load_profile

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _load_send_enabled() -> bool:
    """Read ``email.send_enabled`` from ``data/profile/answers_seed.yaml``.

    Returns False if the file is missing, unreadable, or the key is absent —
    defaulting to the safe "do not send" posture.
    """
    seed_path: Path = settings.profiles_dir / "answers_seed.yaml"
    if not seed_path.exists():
        return False
    try:
        data = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
        return bool(data.get("email", {}).get("send_enabled", False))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "email_apply.answers_seed_read_error",
            path=str(seed_path),
            error=str(exc),
        )
        return False


def _extract_email_address(apply_url: str) -> str | None:
    """Strip the ``mailto:`` scheme and any query string; return the address.

    Returns None if the result is not a plausible email address.
    """
    if not apply_url.startswith("mailto:"):
        return None
    addr = apply_url.removeprefix("mailto:").split("?")[0].strip()
    return addr if "@" in addr else None


def _build_mime_message(
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    resume_path: Path,
) -> MIMEMultipart:
    """Assemble a MIME multipart message with a plain-text body and PDF attachment."""
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with resume_path.open("rb") as fh:
        pdf_part = MIMEApplication(fh.read(), Name=resume_path.name)
    pdf_part["Content-Disposition"] = (
        f'attachment; filename="{resume_path.name}"'
    )
    msg.attach(pdf_part)
    return msg


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class EmailApplyHandler(ApplyHandlerBase):
    """Apply handler for jobs that request applications by email (mailto: links)."""

    name = "email_apply"
    ats_type = "email"

    def can_handle(self, job: Job) -> bool:
        """Return True only for email-type jobs with a valid mailto: apply_url."""
        return bool(
            job.ats_type == "email"
            and job.apply_url
            and job.apply_url.startswith("mailto:")
        )

    async def apply(  # noqa: PLR0912
        self,
        job: Job,
        app: Application,
        package_dir: Path,
        shadow_mode: bool,
        manual_mode: bool = False,
    ) -> ApplyResult:
        """Execute the email application flow.

        Never raises — all exceptions are caught and surfaced via ApplyResult.
        """

        # ── Step 1: Check send_enabled ───────────────────────────────────────
        send_enabled = _load_send_enabled()
        if not send_enabled:
            log.info(
                "email_apply.disabled",
                job_id=job.id,
                reason="email.send_enabled is false in answers_seed.yaml",
            )
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="skipped",
                error=(
                    "email send disabled in answers_seed.yaml; "
                    "set email.send_enabled: true to enable"
                ),
            )

        # ── Step 2: Extract recipient address ────────────────────────────────
        try:
            to_addr = _extract_email_address(job.apply_url or "")
            if not to_addr:
                return ApplyResult(
                    success=False,
                    application_id=app.id,
                    status="failed",
                    error=(
                        f"Could not parse a valid email address from "
                        f"apply_url: {job.apply_url!r}"
                    ),
                )
        except Exception as exc:
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=f"apply_url parse error: {exc}",
            )

        # ── Step 3: Load candidate profile ───────────────────────────────────
        try:
            profile = load_profile()
        except Exception as exc:
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=f"profile load failed: {exc}",
            )

        # ── Step 4: Locate cover letter ──────────────────────────────────────
        cl_path = package_dir / "cover_letter.txt"
        if not cl_path.exists():
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=f"cover_letter.txt not found in {package_dir}",
            )

        # ── Step 5: Locate resume PDF ────────────────────────────────────────
        resume_path = package_dir / "resume.pdf"
        if not resume_path.exists() and app.resume_path:
            resume_path = Path(app.resume_path)
        if not resume_path.exists():
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=(
                    "resume.pdf not found in package_dir and "
                    "app.resume_path is not set or missing"
                ),
            )

        # ── Step 6: Build email ───────────────────────────────────────────────
        try:
            cover_letter_text = cl_path.read_text(encoding="utf-8")
            subject = f"Application for {job.title} \u2013 {profile.contact.name}"
            from_addr = profile.contact.email
            msg = _build_mime_message(
                from_addr=from_addr,
                to_addr=to_addr,
                subject=subject,
                body=cover_letter_text,
                resume_path=resume_path,
            )
        except Exception as exc:
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=f"email build failed: {type(exc).__name__}: {exc}",
            )

        # ── Step 7: Shadow mode gate ──────────────────────────────────────────
        if shadow_mode:
            log.info(
                "email_apply.shadow_stopped",
                job_id=job.id,
                to=to_addr,
                subject=subject,
            )
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="shadow_stopped",
                error="shadow mode active: email was NOT sent",
            )

        # ── Step 8: Send via SMTP ─────────────────────────────────────────────
        try:
            smtp_host: str = os.environ.get("JOBAGENT_SMTP_HOST", "smtp.gmail.com")
            smtp_port: int = int(os.environ.get("JOBAGENT_SMTP_PORT", "587"))
            smtp_user: str = os.environ.get("JOBAGENT_SMTP_USER", from_addr)
            smtp_pass: str = os.environ.get("JOBAGENT_SMTP_PASS", "")

            if not smtp_pass:
                return ApplyResult(
                    success=False,
                    application_id=app.id,
                    status="failed",
                    error=(
                        "SMTP password not configured. "
                        "Set the JOBAGENT_SMTP_PASS environment variable "
                        "(use an app-password for Gmail)."
                    ),
                )

            log.info(
                "email_apply.sending",
                job_id=job.id,
                to=to_addr,
                subject=subject,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
            )

            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(smtp_user, smtp_pass)
                smtp.sendmail(from_addr, [to_addr], msg.as_bytes())

            log.info("email_apply.sent", job_id=job.id, to=to_addr)
            return ApplyResult(
                success=True,
                application_id=app.id,
                status="submitted",
                confirmation_text=f"Email sent to {to_addr}",
            )

        except Exception as exc:  # noqa: BLE001
            log.exception("email_apply.send_error", job_id=job.id, error=str(exc))
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=(
                    f"SMTP send failed: {type(exc).__name__}: {exc}\n"
                    f"{traceback.format_exc()}"
                ),
            )
