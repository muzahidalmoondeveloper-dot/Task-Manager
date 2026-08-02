import logging
import smtplib
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.email_notification_log import EmailNotificationLog

logger = logging.getLogger("email_service")

SMTP_TIMEOUT_SECONDS = 10

# ─── Priority colours used inside HTML templates ──────────────────────────────
_PRIORITY_COLOUR = {
    "high": ("#fef2f2", "#dc2626", "High"),
    "medium": ("#fffbeb", "#d97706", "Medium"),
    "low": ("#f0fdf4", "#16a34a", "Low"),
}

# ─── Status display labels ────────────────────────────────────────────────────
_STATUS_LABEL = {
    "todo": "To Do",
    "in_progress": "In Progress",
    "pending_review": "Pending Review",
    "done": "Done",
}


def _fmt_date(d) -> str:
    if d is None:
        return "Not set"
    if isinstance(d, date):
        return d.strftime("%B %d, %Y")
    return str(d)


class EmailService:

    # ═══════════════════════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════════
    # Organization invitation email
    # ═══════════════════════════════════════════════════════════════════════════

    def send_invitation_email(
        self,
        *,
        to_email: str,
        org_name: str,
        inviter_name: str,
        token: str,
        role: str = "",
    ) -> None:
        from app.core.config import get_settings
        frontend_url = get_settings().FRONTEND_URL
        accept_url = f"{frontend_url}/accept-invitation?token={token}"

        if not settings.SMTP_HOST or not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
            logger.info(
                "[DEV INVITE] To: %s | Org: %s | Inviter: %s | Accept URL: %s",
                to_email, org_name, inviter_name, accept_url,
            )
            return

        subject = f"You've been invited to join {org_name}"
        role_label = role.replace("_", " ").title() if role else ""

        html = self._build_html(
            subject=subject,
            headline="You're invited!",
            body_paragraphs=[
                f"<strong>{inviter_name}</strong> has invited you to join <strong>{org_name}</strong>"
                + (f" as <strong>{role_label}</strong>" if role_label else "")
                + ".",
                "Already have an account? Log in to accept the invitation below. "
                "New here? You'll be asked to create a free account first — either way, "
                "the button below takes you to the right next step.",
            ],
            details=[
                ("Organization", org_name),
                ("Invited by", inviter_name),
                ("Role", role_label),
            ],
            cta_url=accept_url,
            cta_label="View Invitation",
        )

        success, err = self._send_smtp(
            to_email=to_email,
            subject=subject,
            html_body=html,
            event_type="org_invitation",
        )
        if not success:
            logger.warning("Invitation email failed for %s: %s", to_email, err)

    def send_client_invitation_email(
        self,
        *,
        to_email: str,
        org_name: str,
        project_name: str,
        inviter_name: str,
        token: str,
        client_name: str | None = None,
        message: str | None = None,
        template_name: str | None = None,
    ) -> None:
        from app.core.config import get_settings
        frontend_url = get_settings().FRONTEND_URL
        accept_url = f"{frontend_url}/accept-invitation?token={token}"

        if not settings.SMTP_HOST or not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
            logger.info(
                "[DEV CLIENT INVITE] To: %s | Project: %s | Inviter: %s | Accept URL: %s",
                to_email, project_name, inviter_name, accept_url,
            )
            return

        subject = f"You've been invited to view {project_name}"
        greeting = f"Hi {client_name.split()[0]}, " if client_name else ""

        body_paragraphs = [
            f"{greeting}<strong>{inviter_name}</strong> has invited you to view the progress of "
            f"<strong>{project_name}</strong> at <strong>{org_name}</strong>.",
            "Click the button below to set up your account — you'll be able to view "
            "the project's status, complete your onboarding checklist, and submit task requests.",
        ]
        if message:
            body_paragraphs.append(f"<em>\"{message}\"</em>")

        details = [
            ("Project", project_name),
            ("Organization", org_name),
            ("Invited by", inviter_name),
        ]
        if template_name:
            details.append(("Onboarding checklist", template_name))

        html = self._build_html(
            subject=subject,
            headline="You're invited to track a project!",
            body_paragraphs=body_paragraphs,
            details=details,
            cta_url=accept_url,
            cta_label="View Invitation",
        )

        success, err = self._send_smtp(
            to_email=to_email,
            subject=subject,
            html_body=html,
            event_type="client_invitation",
        )
        if not success:
            logger.warning("Client invitation email failed for %s: %s", to_email, err)

    # ═══════════════════════════════════════════════════════════════════════════
    # Existing OTP email (unchanged)
    # ═══════════════════════════════════════════════════════════════════════════

    def send_otp_email(self, *, to_email: str, otp_code: str, purpose: str) -> None:
        if not settings.SMTP_HOST or not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
            logger.info("[DEV OTP] Email: %s | Purpose: %s | OTP: %s", to_email, purpose, otp_code)
            return

        _purpose_map = {
            "register":       ("Verify your account",   "Complete your registration"),
            "login":          ("Confirm your login",     "Verify it's really you"),
            "reset_password": ("Reset your password",   "Password reset requested"),
        }
        subject, headline = _purpose_map.get(purpose, ("Verification code", "Your verification code"))

        # Large, prominent OTP block rendered as an accent card
        otp_block = (
            '<table cellpadding="0" cellspacing="0" border="0" width="100%"'
            ' style="background:#f8fafc;border:2px dashed #cbd5e1;'
            'border-radius:12px;margin:24px 0;">'
            "<tr><td style=\"padding:28px;text-align:center;\">"
            "<p style=\"margin:0 0 8px 0;font-size:12px;color:#64748b;font-weight:700;"
            "letter-spacing:1px;text-transform:uppercase;\">Your verification code</p>"
            f"<p style=\"margin:0;font-size:44px;font-weight:700;color:#0f172a;"
            f"letter-spacing:12px;font-family:'Courier New',Courier,monospace;\">{otp_code}</p>"
            "<p style=\"margin:10px 0 0 0;font-size:12px;color:#94a3b8;\">Expires in 10 minutes</p>"
            "</td></tr></table>"
        )

        html = self._build_html(
            subject=subject,
            headline=headline,
            body_paragraphs=[
                "Use the code below to complete your verification.",
                "If you did not request this, you can safely ignore this email.",
            ],
            details=[],
            accent_block=otp_block,
        )

        plain = (
            f"{headline}\n\n"
            f"Your verification code is: {otp_code}\n\n"
            "This code will expire in 10 minutes.\n\n"
            "If you did not request this, please ignore this email."
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = to_email
        # plain must come first; HTML last so clients that support it prefer HTML
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        try:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.starttls()
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                server.send_message(msg)
            logger.info("OTP email sent | purpose=%s | to=%s", purpose, to_email)
        except Exception as exc:
            logger.error("SMTP OTP delivery failed for %s: %s", to_email, exc)
            logger.info("[FALLBACK OTP] Email: %s | Purpose: %s | OTP: %s", to_email, purpose, otp_code)

    # ═══════════════════════════════════════════════════════════════════════════
    # Private: HTML template builder
    # ═══════════════════════════════════════════════════════════════════════════

    def _build_html(
        self,
        *,
        subject: str,
        headline: str,
        body_paragraphs: list[str],
        details: list[tuple[str, str]],
        cta_url: str = "",
        cta_label: str = "",
        accent_block: str = "",
    ) -> str:
        """Return a complete HTML email string."""

        detail_rows = ""
        for label, value in details:
            if value and value not in ("", "—", "Not set"):
                detail_rows += (
                    f'<tr>'
                    f'<td style="padding:7px 16px 7px 0;color:#64748b;font-size:13px;'
                    f'font-weight:600;white-space:nowrap;vertical-align:top;width:130px;">'
                    f'{label}</td>'
                    f'<td style="padding:7px 0;color:#0f172a;font-size:13px;'
                    f'vertical-align:top;word-break:break-word;">{value}</td>'
                    f'</tr>'
                )

        details_card = ""
        if detail_rows:
            details_card = (
                '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="background:#f8fafc;border-radius:10px;margin:24px 0;">'
                '<tr><td style="padding:18px 22px;">'
                '<table width="100%" cellpadding="0" cellspacing="0" border="0">'
                + detail_rows
                + '</table></td></tr></table>'
            )

        body_html = "".join(
            f'<p style="color:#374151;font-size:15px;line-height:1.65;margin:0 0 12px 0;">'
            f'{p}</p>'
            for p in body_paragraphs
        )

        cta_block = ""
        if cta_url and cta_label:
            cta_block = f"""
        <!-- CTA button -->
        <table cellpadding="0" cellspacing="0" border="0" style="margin-top:28px;">
          <tr>
            <td style="background:#10b981;border-radius:9px;">
              <a href="{cta_url}"
                style="display:inline-block;padding:13px 30px;color:white;
                  text-decoration:none;font-weight:600;font-size:15px;
                  letter-spacing:-0.1px;">{cta_label} &rarr;</a>
            </td>
          </tr>
        </table>

        <p style="color:#94a3b8;font-size:12px;margin:28px 0 0 0;line-height:1.5;">
          If the button doesn't work, copy this link into your browser:<br>
          <a href="{cta_url}" style="color:#10b981;word-break:break-all;">{cta_url}</a>
        </p>"""

        app_name = settings.APP_NAME

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" border="0"
  style="background:#f1f5f9;min-height:100vh;">
<tr><td align="center" style="padding:40px 16px;">

  <table width="600" cellpadding="0" cellspacing="0" border="0"
    style="max-width:600px;width:100%;">

    <!-- ── Header ─────────────────────────────────────────── -->
    <tr>
      <td style="background:linear-gradient(135deg,#059669 0%,#10b981 100%);
        border-radius:14px 14px 0 0;padding:24px 32px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td>
              <span style="color:white;font-size:20px;font-weight:700;
                letter-spacing:-0.4px;">{app_name}</span>
            </td>
            <td align="right">
              <span style="color:rgba(255,255,255,0.75);font-size:12px;">
                Task Management
              </span>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- ── Body ───────────────────────────────────────────── -->
    <tr>
      <td style="background:white;padding:36px 32px 32px 32px;
        border-radius:0 0 14px 14px;
        box-shadow:0 4px 20px rgba(0,0,0,0.07);">

        <h1 style="color:#0f172a;font-size:22px;font-weight:700;margin:0 0 18px 0;
          line-height:1.35;">{headline}</h1>

        {body_html}

        {accent_block}

        {details_card}

        {cta_block}
      </td>
    </tr>

    <!-- ── Footer ─────────────────────────────────────────── -->
    <tr>
      <td style="padding:22px 0 4px 0;text-align:center;">
        <p style="color:#94a3b8;font-size:12px;margin:0;line-height:1.6;">
          Sent by <strong>{app_name}</strong>
          &nbsp;&bull;&nbsp;
          You're receiving this because you're part of this workspace.
        </p>
      </td>
    </tr>

  </table>
</td></tr>
</table>
</body>
</html>"""

    # ═══════════════════════════════════════════════════════════════════════════
    # Private: SMTP send
    # ═══════════════════════════════════════════════════════════════════════════

    def _send_smtp(
        self,
        *,
        to_email: str,
        subject: str,
        html_body: str,
        event_type: str = "unknown",
    ) -> tuple[bool, str]:
        """Send via SMTP. Returns (success, error_message)."""
        # Always log the resolved SMTP config so we can confirm the worker
        # loaded the right .env — never log the password.
        logger.info(
            "SMTP attempt | event=%s | host=%s | port=%s | from=%s | to=%s",
            event_type, settings.SMTP_HOST, settings.SMTP_PORT,
            settings.SMTP_FROM_EMAIL, to_email,
        )

        if not settings.SMTP_HOST or not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
            logger.warning(
                "SMTP credentials not configured — logging to console only"
                " | event=%s | to=%s | subject=%s"
                " | SMTP_HOST=%r | SMTP_USERNAME=%r | SMTP_PASSWORD_set=%s",
                event_type, to_email, subject,
                settings.SMTP_HOST, settings.SMTP_USERNAME,
                bool(settings.SMTP_PASSWORD),
            )
            return False, "SMTP credentials not configured"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.starttls()
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                server.send_message(msg)
            logger.info(
                "SMTP sent | event=%s | host=%s | to=%s | subject=%s",
                event_type, settings.SMTP_HOST, to_email, subject,
            )
            return True, ""
        except Exception as exc:
            err = str(exc)
            logger.exception(
                "SMTP error | event=%s | host=%s | port=%s | to=%s",
                event_type, settings.SMTP_HOST, settings.SMTP_PORT, to_email,
            )
            return False, err

    # ═══════════════════════════════════════════════════════════════════════════
    # Private: deduplication + logging
    # ═══════════════════════════════════════════════════════════════════════════

    async def _is_duplicate(self, db: AsyncSession, dedup_key: str) -> bool:
        result = await db.execute(
            select(EmailNotificationLog).where(
                EmailNotificationLog.deduplication_key == dedup_key
            )
        )
        return result.scalar_one_or_none() is not None

    async def _log(
        self,
        db: AsyncSession,
        *,
        task_id: int | None,
        recipient_user_id: int | None,
        recipient_email: str,
        event_type: str,
        dedup_key: str,
        success: bool,
        error_message: str = "",
    ) -> None:
        entry = EmailNotificationLog(
            task_id=task_id,
            recipient_user_id=recipient_user_id,
            recipient_email=recipient_email,
            event_type=event_type,
            deduplication_key=dedup_key,
            status="sent" if success else "failed",
            error_message=error_message or None,
            sent_at=datetime.now(timezone.utc) if success else None,
        )
        db.add(entry)
        try:
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning("Could not write email log for key=%s: %s", dedup_key, exc)

    # ═══════════════════════════════════════════════════════════════════════════
    # Task notification emails
    # ═══════════════════════════════════════════════════════════════════════════

    async def send_task_assigned(
        self,
        db: AsyncSession,
        *,
        task,
        assignee,
        assigned_by,
    ) -> None:
        """Send 'task assigned' email to the assignee (NOT the assigner)."""
        # Always log every attempt with full participant detail for auditability
        logger.info(
            "send_task_assigned INIT | event=task_assigned"
            " | task_id=%s | assigner_id=%s | assigner_email=%s"
            " | assignee_id=%s | recipient_email=%s",
            task.id, assigned_by.id, assigned_by.email,
            assignee.id, assignee.email,
        )

        today = date.today().isoformat()
        dedup_key = f"task_assigned:{task.id}:{assignee.email}:{today}"

        try:
            if await self._is_duplicate(db, dedup_key):
                logger.info(
                    "send_task_assigned SKIP (duplicate) | task_id=%s | assignee_id=%s | to=%s",
                    task.id, assignee.id, assignee.email,
                )
                return

            team_name = (task.team.name if task.team else None) or "—"
            project_name = (task.project.name if task.project else None) or "—"
            priority = (getattr(task, "priority", None) or "medium").lower()
            p_bg, p_fg, p_label = _PRIORITY_COLOUR.get(priority, _PRIORITY_COLOUR["medium"])
            status_label = _STATUS_LABEL.get(task.status, task.status)

            priority_badge = (
                f'<span style="display:inline-block;padding:3px 10px;'
                f'background:{p_bg};color:{p_fg};border-radius:20px;'
                f'font-size:12px;font-weight:600;">{p_label}</span>'
            )

            subject = f"New Task Assigned: {task.name}"
            html = self._build_html(
                subject=subject,
                headline="You have a new task",
                body_paragraphs=[
                    f"Hi {assignee.full_name},",
                    f"<strong>{assigned_by.full_name}</strong> has assigned you a new task. "
                    "Please review the details below and get started when you're ready.",
                ],
                details=[
                    ("Task", task.name),
                    ("Status", status_label),
                    ("Priority", priority_badge),
                    ("Due Date", _fmt_date(task.due_date)),
                    ("Team", team_name),
                    ("Project", project_name),
                    ("Assigned By", assigned_by.full_name),
                ],
                cta_url=f"{settings.FRONTEND_BASE_URL}/tasks",
                cta_label="View My Tasks",
            )

            # Recipient is explicitly the ASSIGNEE — never the assigner
            success, err = self._send_smtp(
                to_email=assignee.email,
                subject=subject,
                html_body=html,
                event_type="task_assigned",
            )
            await self._log(
                db,
                task_id=task.id,
                recipient_user_id=assignee.id,
                recipient_email=assignee.email,
                event_type="task_assigned",
                dedup_key=dedup_key,
                success=success,
                error_message=err,
            )
            logger.info(
                "send_task_assigned DONE | task_id=%s | assigner_id=%s | assignee_id=%s | to=%s | success=%s",
                task.id, assigned_by.id, assignee.id, assignee.email, success,
            )

        except Exception as exc:
            logger.exception("send_task_assigned FAILED | task_id=%s: %s", task.id, exc)

    # ─────────────────────────────────────────────────────────────────────────

    async def send_due_date_updated(
        self,
        db: AsyncSession,
        *,
        task,
        assignee,
        updated_by,
        old_due_date,
    ) -> None:
        """Send 'due date changed' email to the current assignee."""
        new_due = task.due_date
        dedup_key = f"task_due_updated:{task.id}:{assignee.email}:{_fmt_date(new_due)}"

        try:
            if await self._is_duplicate(db, dedup_key):
                logger.info("Duplicate skip | task_due_updated | task=%s | to=%s", task.id, assignee.email)
                return

            team_name = (task.team.name if task.team else None) or "—"
            project_name = (task.project.name if task.project else None) or "—"
            status_label = _STATUS_LABEL.get(task.status, task.status)

            old_str = _fmt_date(old_due_date)
            new_str = _fmt_date(new_due)

            change_block = (
                '<table cellpadding="0" cellspacing="0" border="0" '
                'style="background:#fefce8;border-left:4px solid #eab308;'
                'border-radius:0 8px 8px 0;padding:0;margin:20px 0;width:100%;">'
                '<tr><td style="padding:14px 18px;">'
                f'<p style="margin:0;font-size:13px;color:#713f12;font-weight:600;">'
                f'Due date changed</p>'
                f'<p style="margin:4px 0 0 0;font-size:13px;color:#854d0e;">'
                f'<span style="text-decoration:line-through;color:#a16207;">{old_str}</span>'
                f' &rarr; <strong>{new_str}</strong></p>'
                '</td></tr></table>'
            )

            subject = f"Due Date Updated: {task.name}"
            html = self._build_html(
                subject=subject,
                headline="A task due date has been updated",
                body_paragraphs=[
                    f"Hi {assignee.full_name},",
                    f"<strong>{updated_by.full_name}</strong> has updated the due date "
                    f"for your task <strong>{task.name}</strong>.",
                ],
                details=[
                    ("Task", task.name),
                    ("Status", status_label),
                    ("New Due Date", new_str),
                    ("Team", team_name),
                    ("Project", project_name),
                    ("Updated By", updated_by.full_name),
                ],
                cta_url=f"{settings.FRONTEND_BASE_URL}/tasks",
                cta_label="View Task",
                accent_block=change_block,
            )

            success, err = self._send_smtp(
                to_email=assignee.email,
                subject=subject,
                html_body=html,
                event_type="task_due_updated",
            )
            await self._log(
                db,
                task_id=task.id,
                recipient_user_id=assignee.id,
                recipient_email=assignee.email,
                event_type="task_due_updated",
                dedup_key=dedup_key,
                success=success,
                error_message=err,
            )
            logger.info(
                "send_due_date_updated DONE | task_id=%s | assignee_id=%s | to=%s | success=%s",
                task.id, assignee.id, assignee.email, success,
            )

        except Exception as exc:
            logger.exception("send_due_date_updated FAILED | task_id=%s: %s", task.id, exc)

    # ─────────────────────────────────────────────────────────────────────────

    async def send_task_sent_for_review(
        self,
        db: AsyncSession,
        *,
        task,
        assignee,
        manager,
    ) -> None:
        """Notify team manager (or admin fallback) that an assignee submitted a task for review."""
        logger.info(
            "send_task_sent_for_review INIT | event=task_sent_for_review"
            " | task_id=%s | submitter_id=%s | submitter_email=%s"
            " | reviewer_id=%s | recipient_email=%s",
            task.id, assignee.id, assignee.email, manager.id, manager.email,
        )

        today = date.today().isoformat()
        dedup_key = f"task_sent_review:{task.id}:{manager.email}:{today}"

        try:
            if await self._is_duplicate(db, dedup_key):
                logger.info(
                    "send_task_sent_for_review SKIP (duplicate) | task_id=%s | to=%s",
                    task.id, manager.email,
                )
                return

            team_name = (task.team.name if task.team else None) or "—"
            project_name = (task.project.name if task.project else None) or "—"

            subject = f"Task Ready for Review: {task.name}"
            html = self._build_html(
                subject=subject,
                headline="A task is waiting for your review",
                body_paragraphs=[
                    f"Hi {manager.full_name},",
                    f"<strong>{assignee.full_name}</strong> has submitted the task "
                    f"<strong>{task.name}</strong> for your review and approval.",
                    "Please review the task and either approve it or send it back with feedback.",
                ],
                details=[
                    ("Task", task.name),
                    ("Submitted By", assignee.full_name),
                    ("Due Date", _fmt_date(task.due_date)),
                    ("Team", team_name),
                    ("Project", project_name),
                ],
                cta_url=f"{settings.FRONTEND_BASE_URL}/tasks",
                cta_label="Review Task",
            )

            success, err = self._send_smtp(
                to_email=manager.email,
                subject=subject,
                html_body=html,
                event_type="task_sent_for_review",
            )
            await self._log(
                db,
                task_id=task.id,
                recipient_user_id=manager.id,
                recipient_email=manager.email,
                event_type="task_sent_for_review",
                dedup_key=dedup_key,
                success=success,
                error_message=err,
            )
            logger.info(
                "send_task_sent_for_review DONE | task_id=%s | submitter_id=%s | reviewer_id=%s | to=%s | success=%s",
                task.id, assignee.id, manager.id, manager.email, success,
            )

        except Exception as exc:
            logger.exception("send_task_sent_for_review FAILED | task_id=%s: %s", task.id, exc)

    # ─────────────────────────────────────────────────────────────────────────

    async def send_task_approved(
        self,
        db: AsyncSession,
        *,
        task,
        assignee,
        approved_by,
    ) -> None:
        """Notify the assignee that the manager approved their task."""
        today = date.today().isoformat()
        dedup_key = f"task_approved:{task.id}:{assignee.email}:{today}"

        try:
            if await self._is_duplicate(db, dedup_key):
                logger.info("Duplicate skip | task_approved | task=%s | to=%s", task.id, assignee.email)
                return

            team_name = (task.team.name if task.team else None) or "—"
            project_name = (task.project.name if task.project else None) or "—"
            review_note = task.review_note or ""

            congrats_block = (
                '<table cellpadding="0" cellspacing="0" border="0" '
                'style="background:#f0fdf4;border-left:4px solid #22c55e;'
                'border-radius:0 8px 8px 0;margin:20px 0;width:100%;">'
                '<tr><td style="padding:14px 18px;">'
                '<p style="margin:0;font-size:14px;color:#15803d;font-weight:600;">'
                '&#10003;&nbsp; Task Approved &amp; Completed!</p>'
                + (
                    f'<p style="margin:6px 0 0 0;font-size:13px;color:#166534;">'
                    f'Note from {approved_by.full_name}: {review_note}</p>'
                    if review_note and review_note != "Approved"
                    else ""
                )
                + '</td></tr></table>'
            )

            subject = f"Task Approved: {task.name}"
            html = self._build_html(
                subject=subject,
                headline="Great work — your task has been approved!",
                body_paragraphs=[
                    f"Hi {assignee.full_name},",
                    f"<strong>{approved_by.full_name}</strong> has reviewed and approved "
                    f"your task <strong>{task.name}</strong>. It is now marked as complete.",
                ],
                details=[
                    ("Task", task.name),
                    ("Status", "Done ✓"),
                    ("Approved By", approved_by.full_name),
                    ("Team", team_name),
                    ("Project", project_name),
                ],
                cta_url=f"{settings.FRONTEND_BASE_URL}/tasks",
                cta_label="View Completed Tasks",
                accent_block=congrats_block,
            )

            success, err = self._send_smtp(
                to_email=assignee.email,
                subject=subject,
                html_body=html,
                event_type="task_approved",
            )
            await self._log(
                db,
                task_id=task.id,
                recipient_user_id=assignee.id,
                recipient_email=assignee.email,
                event_type="task_approved",
                dedup_key=dedup_key,
                success=success,
                error_message=err,
            )
            logger.info(
                "send_task_approved DONE | task_id=%s | approver_id=%s | recipient_id=%s | to=%s | success=%s",
                task.id, approved_by.id, assignee.id, assignee.email, success,
            )

        except Exception as exc:
            logger.exception("send_task_approved FAILED | task_id=%s: %s", task.id, exc)

    # ─────────────────────────────────────────────────────────────────────────

    async def send_task_assigned_back(
        self,
        db: AsyncSession,
        *,
        task,
        assignee,
        manager,
        note: str,
    ) -> None:
        """Notify assignee that the manager returned the task for more work."""
        today = date.today().isoformat()
        dedup_key = f"task_assigned_back:{task.id}:{assignee.email}:{today}"

        try:
            if await self._is_duplicate(db, dedup_key):
                logger.info("Duplicate skip | task_assigned_back | task=%s | to=%s", task.id, assignee.email)
                return

            team_name = (task.team.name if task.team else None) or "—"
            project_name = (task.project.name if task.project else None) or "—"

            feedback_block = (
                '<table cellpadding="0" cellspacing="0" border="0" '
                'style="background:#fef2f2;border-left:4px solid #ef4444;'
                'border-radius:0 8px 8px 0;margin:20px 0;width:100%;">'
                '<tr><td style="padding:14px 18px;">'
                f'<p style="margin:0;font-size:13px;color:#991b1b;font-weight:600;">'
                f'Feedback from {manager.full_name}</p>'
                f'<p style="margin:6px 0 0 0;font-size:14px;color:#7f1d1d;">{note}</p>'
                '</td></tr></table>'
            )

            subject = f"Task Returned for Revision: {task.name}"
            html = self._build_html(
                subject=subject,
                headline="Your task needs a little more work",
                body_paragraphs=[
                    f"Hi {assignee.full_name},",
                    f"<strong>{manager.full_name}</strong> has reviewed your task "
                    f"<strong>{task.name}</strong> and sent it back for further work. "
                    "Please review the feedback below and resubmit when ready.",
                ],
                details=[
                    ("Task", task.name),
                    ("Status", "In Progress"),
                    ("Due Date", _fmt_date(task.due_date)),
                    ("Team", team_name),
                    ("Project", project_name),
                    ("Reviewed By", manager.full_name),
                ],
                cta_url=f"{settings.FRONTEND_BASE_URL}/tasks",
                cta_label="View Task",
                accent_block=feedback_block,
            )

            success, err = self._send_smtp(
                to_email=assignee.email,
                subject=subject,
                html_body=html,
                event_type="task_assigned_back",
            )
            await self._log(
                db,
                task_id=task.id,
                recipient_user_id=assignee.id,
                recipient_email=assignee.email,
                event_type="task_assigned_back",
                dedup_key=dedup_key,
                success=success,
                error_message=err,
            )
            logger.info(
                "send_task_assigned_back DONE | task_id=%s | manager_id=%s | assignee_id=%s | to=%s | success=%s",
                task.id, manager.id, assignee.id, assignee.email, success,
            )

        except Exception as exc:
            logger.exception("send_task_assigned_back FAILED | task_id=%s: %s", task.id, exc)

    async def send_client_task_request(
        self,
        db: AsyncSession,
        *,
        task_request,
        recipient,
        submitted_by,
    ) -> None:
        """Notify a Project Manager / Owner / Admin that a client submitted a task request."""
        today = date.today().isoformat()
        dedup_key = f"client_task_request:{task_request.id}:{recipient.email}:{today}"

        try:
            if await self._is_duplicate(db, dedup_key):
                logger.info("Duplicate skip | client_task_request | request=%s | to=%s", task_request.id, recipient.email)
                return

            project_name = (task_request.project.name if task_request.project else None) or "—"

            subject = f"New Task Request: {task_request.title}"
            html = self._build_html(
                subject=subject,
                headline="A client submitted a new task request",
                body_paragraphs=[
                    f"Hi {recipient.full_name},",
                    f"<strong>{submitted_by.full_name}</strong> submitted a new task request "
                    f"for project <strong>{project_name}</strong>. Review it and convert it into a "
                    "task when ready.",
                ],
                details=[
                    ("Request", task_request.title),
                    ("Project", project_name),
                    ("Submitted By", submitted_by.full_name),
                ],
                cta_url=f"{settings.FRONTEND_BASE_URL}/projects/{task_request.project_id}",
                cta_label="Review Task Request",
            )

            success, err = self._send_smtp(
                to_email=recipient.email,
                subject=subject,
                html_body=html,
                event_type="client_task_request",
            )
            await self._log(
                db,
                task_id=None,
                recipient_user_id=recipient.id,
                recipient_email=recipient.email,
                event_type="client_task_request",
                dedup_key=dedup_key,
                success=success,
                error_message=err,
            )
            logger.info(
                "send_client_task_request DONE | request_id=%s | to=%s | success=%s",
                task_request.id, recipient.email, success,
            )
        except Exception as exc:
            logger.exception("send_client_task_request FAILED | request_id=%s: %s", task_request.id, exc)

    async def send_task_request_reviewed(
        self,
        db: AsyncSession,
        *,
        task_request,
        client,
        reviewed_by,
        approved: bool,
    ) -> None:
        """Notify the client whose task request was converted into a task, or rejected."""
        today = date.today().isoformat()
        dedup_key = f"task_request_reviewed:{task_request.id}:{client.email}:{today}"

        try:
            if await self._is_duplicate(db, dedup_key):
                logger.info("Duplicate skip | task_request_reviewed | request=%s | to=%s", task_request.id, client.email)
                return

            project_name = (task_request.project.name if task_request.project else None) or "—"
            subject = (
                f"Task Request Approved: {task_request.title}" if approved
                else f"Task Request Declined: {task_request.title}"
            )
            headline = "Your task request was approved" if approved else "Your task request was declined"
            body = (
                f"<strong>{reviewed_by.full_name}</strong> reviewed your task request and created a task from it."
                if approved else
                f"<strong>{reviewed_by.full_name}</strong> reviewed your task request and was unable to proceed with it."
            )

            html = self._build_html(
                subject=subject,
                headline=headline,
                body_paragraphs=[f"Hi {client.full_name},", body],
                details=[
                    ("Request", task_request.title),
                    ("Project", project_name),
                    ("Reviewed By", reviewed_by.full_name),
                ],
                cta_url=f"{settings.FRONTEND_BASE_URL}/client/projects/{task_request.project_id}",
                cta_label="View Project",
            )

            success, err = self._send_smtp(
                to_email=client.email,
                subject=subject,
                html_body=html,
                event_type="task_request_reviewed",
            )
            await self._log(
                db,
                task_id=None,
                recipient_user_id=client.id,
                recipient_email=client.email,
                event_type="task_request_reviewed",
                dedup_key=dedup_key,
                success=success,
                error_message=err,
            )
            logger.info(
                "send_task_request_reviewed DONE | request_id=%s | to=%s | success=%s",
                task_request.id, client.email, success,
            )
        except Exception as exc:
            logger.exception("send_task_request_reviewed FAILED | request_id=%s: %s", task_request.id, exc)

    # ─────────────────────────────────────────────────────────────────────────

    async def send_due_date_reminder(
        self,
        db: AsyncSession,
        *,
        task,
        recipient,
        window: str,
        role_label: str = "assignee",
    ) -> None:
        """
        Send a due-date reminder email.

        window: "today" | "tomorrow"
        role_label: "assignee" | "manager"  — shown in dedup key and subject
        """
        due = task.due_date
        dedup_key = f"task_due_{window}:{task.id}:{recipient.email}:{_fmt_date(due)}"

        try:
            if await self._is_duplicate(db, dedup_key):
                logger.info(
                    "Duplicate skip | task_due_%s | task=%s | to=%s",
                    window, task.id, recipient.email,
                )
                return

            team_name = (task.team.name if task.team else None) or "—"
            project_name = (task.project.name if task.project else None) or "—"
            assignee_name = (task.assignee.full_name if task.assignee else "—")
            status_label = _STATUS_LABEL.get(task.status, task.status)

            if window == "today":
                headline = "Task due TODAY"
                urgency_colour = "#dc2626"
                urgency_bg = "#fef2f2"
                urgency_border = "#ef4444"
                window_label = "today"
                subject = f"Due Today: {task.name}"
            else:
                headline = "Task due tomorrow"
                urgency_colour = "#d97706"
                urgency_bg = "#fffbeb"
                urgency_border = "#f59e0b"
                window_label = "tomorrow"
                subject = f"Due Tomorrow: {task.name}"

            urgency_block = (
                f'<table cellpadding="0" cellspacing="0" border="0" '
                f'style="background:{urgency_bg};border-left:4px solid {urgency_border};'
                f'border-radius:0 8px 8px 0;margin:20px 0;width:100%;">'
                f'<tr><td style="padding:14px 18px;">'
                f'<p style="margin:0;font-size:14px;color:{urgency_colour};font-weight:700;">'
                f'&#9888;&nbsp; This task is due {window_label}: {_fmt_date(due)}</p>'
                f'</td></tr></table>'
            )

            if role_label == "manager":
                intro = [
                    f"Hi {recipient.full_name},",
                    f"This is a reminder that the task <strong>{task.name}</strong> "
                    f"(assigned to <strong>{assignee_name}</strong>) is due {window_label}.",
                ]
            else:
                intro = [
                    f"Hi {recipient.full_name},",
                    f"This is a friendly reminder that your task <strong>{task.name}</strong> "
                    f"is due {window_label}. Please make sure to complete it on time.",
                ]

            details = [
                ("Task", task.name),
                ("Status", status_label),
                ("Due Date", _fmt_date(due)),
                ("Team", team_name),
                ("Project", project_name),
            ]
            if role_label == "manager":
                details.insert(2, ("Assignee", assignee_name))

            html = self._build_html(
                subject=subject,
                headline=headline,
                body_paragraphs=intro,
                details=details,
                cta_url=f"{settings.FRONTEND_BASE_URL}/tasks",
                cta_label="View Task",
                accent_block=urgency_block,
            )

            event_type = f"task_due_{window}"
            success, err = self._send_smtp(
                to_email=recipient.email,
                subject=subject,
                html_body=html,
                event_type=event_type,
            )
            await self._log(
                db,
                task_id=task.id,
                recipient_user_id=recipient.id,
                recipient_email=recipient.email,
                event_type=event_type,
                dedup_key=dedup_key,
                success=success,
                error_message=err,
            )
            logger.info(
                "send_due_date_reminder DONE | event=%s | task_id=%s | role=%s | recipient_id=%s | to=%s | success=%s",
                event_type, task.id, role_label, recipient.id, recipient.email, success,
            )

        except Exception as exc:
            logger.exception("send_due_date_reminder failed | task=%s: %s", task.id, exc)


email_service = EmailService()
