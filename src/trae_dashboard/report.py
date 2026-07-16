"""Daily email report rendering + SMTP delivery.

Triggered by `trae-dashboard report` (CLI). Intended to be wired to a
Windows Task Scheduler / cron job that runs once a day around 09:00.

Design notes:
  - Reuses `storage.get_model_usage_by_account` so the per-account
    numbers in the email match the dashboard exactly (including the
    Doubao-Seed-Code 0.5 display weight).
  - HTML uses inline CSS only — many email clients (Outlook, QQ Mail)
    strip `<style>` blocks, so we keep styling per-element.
  - SMTP password is read from the environment variable named by
    `EmailConfig.smtp_password_env` (default SMTP_PASSWORD). Never
    hardcode it in config.yaml.
  - Failures raise; the CLI wraps in try/except and prints a non-zero
    exit so the task scheduler surfaces the failure.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy as _email_policy
from email.message import EmailMessage
from email.utils import formatdate

from .config import Config, EmailConfig
from .cycle import current_cycle_window
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class ReportRow:
    """One row in the email table — already weighted, ready to render."""

    display_name: str
    email: str
    input_tokens: int
    output_tokens: int
    consumed: int
    quota_pct: float
    top_model: str
    top_model_consumed: int


def _fmt_tokens(n: int) -> str:
    """Human-friendly token count: 12,345,678 -> '12.3M' / '4,567' -> '4.6K'.

    Keeps the email narrow; the dashboard already shows exact numbers.
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _quota_color(pct: float) -> str:
    """Traffic-light color for the quota cell."""
    if pct >= 90:
        return "#dc2626"  # red-600
    if pct >= 70:
        return "#d97706"  # amber-600
    return "#16a34a"  # green-600


def _esc(s: str) -> str:
    """HTML-escape a string for safe insertion into the email body."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def collect_report_rows(storage: Storage, cfg: Config) -> list[ReportRow]:
    """Build the per-account rows for the current cycle.

    Reads the weighted per-account totals (matches dashboard numbers),
    then for each account fetches the per-model breakdown to pick the
    top-consuming model.
    """
    start_dt, _ = current_cycle_window()
    start_date = start_dt.date().isoformat()
    end_date = datetime.now(timezone.utc).date().isoformat()
    per_q = cfg.per_account_quota

    accounts = storage.get_model_usage_by_account(
        start_date, end_date, cfg.included_model_names
    )
    rows: list[ReportRow] = []
    for a in accounts:
        email = a["email"]
        in_n = int(a["input_tokens"] or 0)
        out_n = int(a["output_tokens"] or 0)
        consumed = in_n + out_n
        pct = round((consumed / per_q) * 100, 1) if per_q > 0 else 0.0

        # Per-model breakdown to find the top model for this account.
        models = storage.get_model_usage_for_account(
            email, start_date, cfg.included_model_names
        )
        if models:
            top = max(models, key=lambda m: m.input_tokens + m.output_tokens)
            top_name = top.model_name
            top_consumed = top.input_tokens + top.output_tokens
        else:
            top_name = "—"
            top_consumed = 0

        rows.append(
            ReportRow(
                display_name=a["display_name"] or email.split("@")[0],
                email=email,
                input_tokens=in_n,
                output_tokens=out_n,
                consumed=consumed,
                quota_pct=pct,
                top_model=top_name,
                top_model_consumed=top_consumed,
            )
        )
    return rows


def render_html(
    rows: list[ReportRow],
    cfg: Config,
    cycle_start: datetime,
    now: datetime,
) -> str:
    """Render the daily report as a self-contained HTML document.

    Uses inline CSS (no `<style>` block) for maximum email-client
    compatibility. Output is a UTF-8 HTML string.
    """
    total_consumed = sum(r.consumed for r in rows)
    # Use the rendered row count (driven by storage) as the denominator,
    # not `len(cfg.accounts)`. Accounts added via the dashboard's
    # POST /api/accounts endpoint are persisted in the DB but never
    # written back to config.yaml — counting them from cfg would silently
    # under-count the company and inflate utilization_pct. This matches
    # the /api/status endpoint's formula.
    total_quota = cfg.per_account_quota * len(rows)
    total_pct = (
        round((total_consumed / total_quota) * 100, 1) if total_quota > 0 else 0.0
    )

    # Cycle dates in UTC for the header (e.g. "2026-06-10 → 2026-07-06").
    cycle_str = f"{cycle_start.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')}"

    # Build table rows
    table_rows_html = []
    for r in rows:
        color = _quota_color(r.quota_pct)
        table_rows_html.append(f"""
        <tr>
          <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;">
            <div style="font-weight:600;color:#111827;">{_esc(r.display_name)}</div>
            <div style="font-size:12px;color:#6b7280;">{_esc(r.email)}</div>
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-variant-numeric:tabular-nums;color:#111827;">
            {_fmt_tokens(r.consumed)}
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-variant-numeric:tabular-nums;color:{color};font-weight:600;">
            {r.quota_pct:.1f}%
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-variant-numeric:tabular-nums;color:#6b7280;">
            {_fmt_tokens(r.input_tokens)} / {_fmt_tokens(r.output_tokens)}
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;font-size:13px;color:#374151;">
            {_esc(r.top_model)}
            <span style="color:#9ca3af;">({_fmt_tokens(r.top_model_consumed)})</span>
          </td>
        </tr>""")

    rows_html = (
        "".join(table_rows_html)
        if table_rows_html
        else (
            '<tr><td colspan="5" style="padding:20px;text-align:center;color:#9ca3af;">'
            "本周期暂无用量数据</td></tr>"
        )
    )

    return f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Trae Dashboard 日报</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,'Microsoft YaHei','微软雅黑','PingFang SC','Hiragino Sans GB','Noto Sans CJK SC',sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="720" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.05);overflow:hidden;">
        <!-- Header -->
        <tr><td style="padding:24px 28px;background:#1e3a8a;">
          <div style="font-size:18px;font-weight:600;color:#ffffff;">Trae Dashboard · 周期消耗日报</div>
          <div style="font-size:13px;color:#bfdbfe;margin-top:4px;">{now.strftime('%Y-%m-%d %H:%M')} UTC · 计费周期 {cycle_str}</div>
        </td></tr>
        <!-- KPI strip -->
        <tr><td style="padding:20px 28px 8px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="width:33%;padding-right:12px;">
                <div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;">总消耗</div>
                <div style="font-size:22px;font-weight:600;color:#111827;margin-top:2px;">{_fmt_tokens(total_consumed)}</div>
              </td>
              <td style="width:33%;padding-right:12px;">
                <div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;">总配额</div>
                <div style="font-size:22px;font-weight:600;color:#111827;margin-top:2px;">{_fmt_tokens(total_quota)}</div>
              </td>
              <td style="width:33%;">
                <div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;">使用率</div>
                <div style="font-size:22px;font-weight:600;color:{_quota_color(total_pct)};margin-top:2px;">{total_pct:.1f}%</div>
              </td>
            </tr>
          </table>
        </td></tr>
        <!-- Table -->
        <tr><td style="padding:8px 20px 20px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:14px;">
            <thead>
              <tr style="background:#f9fafb;">
                <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #e5e7eb;">账号</th>
                <th style="padding:10px;text-align:right;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #e5e7eb;">总消耗</th>
                <th style="padding:10px;text-align:right;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #e5e7eb;">配额占比</th>
                <th style="padding:10px;text-align:right;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #e5e7eb;">Input / Output</th>
                <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #e5e7eb;">Top 模型</th>
              </tr>
            </thead>
            <tbody>{rows_html}
            </tbody>
          </table>
        </td></tr>
        <!-- Footer -->
        <tr><td style="padding:16px 28px;background:#f9fafb;border-top:1px solid #e5e7eb;">
          <div style="font-size:12px;color:#9ca3af;">
            本邮件由 Trae Dashboard 自动发送 · 数据采集自 Trae Enterprise OpenAPI
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_email(
    email_cfg: EmailConfig,
    subject: str,
    html_body: str,
) -> None:
    """Send the HTML email via SMTP_SSL.

    Password is read from the environment variable named by
    `email_cfg.smtp_password_env`. Raises RuntimeError if missing.
    """
    password = os.environ.get(email_cfg.smtp_password_env, "")
    if not password:
        raise RuntimeError(
            f"SMTP password not found in env var '{email_cfg.smtp_password_env}'. "
            "Set it in .env or export it before running."
        )

    msg = _build_message(email_cfg, subject, html_body)

    log.info(
        "sending email via %s:%d from %s to %s",
        email_cfg.smtp_host,
        email_cfg.smtp_port,
        email_cfg.from_addr,
        email_cfg.recipients,
    )
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        email_cfg.smtp_host, email_cfg.smtp_port, context=ctx, timeout=30
    ) as server:
        server.login(email_cfg.smtp_user, password)
        server.send_message(msg)


def _build_message(
    email_cfg: EmailConfig,
    subject: str,
    html_body: str,
) -> EmailMessage:
    """Construct the EmailMessage for both send and .eml export."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg.from_addr
    if email_cfg.recipients:
        msg["To"] = ", ".join(email_cfg.recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content("本邮件为 HTML 格式,请使用支持 HTML 的客户端查看。")
    msg.add_alternative(html_body, subtype="html")
    return msg


def build_eml(
    email_cfg: EmailConfig,
    subject: str,
    html_body: str,
) -> bytes:
    """Serialize the report as an .eml byte string (RFC 822)."""
    return _build_message(email_cfg, subject, html_body).as_bytes(
        policy=_email_policy.SMTPUTF8,
    )


def run_report(
    storage: Storage,
    cfg: Config,
    *,
    recipients_override: list[str] | None = None,
    send: bool = True,
) -> dict:
    """Top-level entry: collect data, render HTML, optionally send email.

    Parameters
    ----------
    recipients_override:
        If provided, use this list as the recipients for THIS send only
        (does not mutate `cfg`). Useful for the frontend "confirm before
        send" dialog where the user may toggle recipients on/off.
        Ignored when `send=False` (preview mode).
    send:
        If False, render the report and return the HTML + summary but
        DO NOT send any email. Used for previewing in the frontend.

    Returns a summary dict for the CLI to print. Raises on failure.
    """
    if send:
        if not cfg.email.enabled:
            raise RuntimeError(
                "email report is disabled in config.yaml (email.enabled: false)"
            )
        # Build a temporary EmailConfig with overridden recipients if requested.
        email_cfg = cfg.email
        if recipients_override is not None:
            from dataclasses import replace as _dc_replace
            email_cfg = _dc_replace(cfg.email, recipients=list(recipients_override))
            if not email_cfg.recipients:
                raise RuntimeError("收件人列表为空，请至少选择一位收件人")
    else:
        # Preview mode: use override recipients for display, but don't
        # enforce the enabled flag (we're not sending anything).
        email_cfg = cfg.email
        if recipients_override is not None:
            from dataclasses import replace as _dc_replace
            email_cfg = _dc_replace(cfg.email, recipients=list(recipients_override))

    rows = collect_report_rows(storage, cfg)
    now = datetime.now(timezone.utc)
    start_dt, _ = current_cycle_window()
    html = render_html(rows, cfg, start_dt, now)
    subject = f"[Trae Dashboard] 周期消耗日报 {now.strftime('%Y-%m-%d')}"

    if send:
        send_email(email_cfg, subject, html)

    return {
        "recipients": list(email_cfg.recipients),
        "recipient_count": len(email_cfg.recipients),
        "rows": len(rows),
        "total_consumed": sum(r.consumed for r in rows),
        "subject": subject,
        "html": html if not send else None,
    }
