from __future__ import annotations

from html import escape


def render_install_success_html(
    account_login: str | None,
    installation_id: int | None,
) -> str:
    return _render_html(
        title="Connected successfully",
        message="Nano GitHub is now connected to GitHub.",
        account_login=account_login,
        installation_id=installation_id,
        success=True,
    )


def render_install_error_html(
    reason: str | None = None,
) -> str:
    return _render_html(
        title="Connection failed",
        message="Return to Discord and press Connect GitHub again.",
        reason=reason or "The setup token was invalid or expired.",
        success=False,
    )


def _render_html(
    *,
    title: str,
    message: str,
    account_login: str | None = None,
    installation_id: int | None = None,
    reason: str | None = None,
    success: bool,
) -> str:
    accent = "#22c55e" if success else "#ef4444"
    safe_title = escape(title)
    safe_message = escape(message)
    detail_rows = []
    if account_login:
        detail_rows.append(
            '<div class="detail"><span>Account</span>'
            f"<strong>{escape(account_login)}</strong></div>"
        )
    if installation_id is not None:
        detail_rows.append(
            '<div class="detail"><span>Installation ID</span>'
            f"<strong>{escape(str(installation_id))}</strong></div>"
        )
    if reason:
        detail_rows.append(f'<p class="reason">{escape(reason)}</p>')
    details = "\n".join(detail_rows)
    hint = (
        "You can now return to Discord and refresh <strong>/github dashboard</strong>."
        if success
        else "Return to Discord and press <strong>Connect GitHub</strong> again."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nano GitHub</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b0f19;
      --card: #111827;
      --border: #1f2937;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --accent: {accent};
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      line-height: 1.5;
    }}
    main {{
      width: min(100%, 440px);
      padding: 28px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
    }}
    .brand {{
      margin: 0 0 18px;
      color: var(--muted);
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0;
      color: var(--text);
      font-size: 28px;
      line-height: 1.15;
      letter-spacing: 0;
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 14px;
      color: var(--accent);
      font-size: 14px;
      font-weight: 700;
    }}
    .dot {{
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--accent);
      box-shadow: 0 0 0 4px rgba(34, 197, 94, 0.12);
    }}
    p {{
      margin: 16px 0 0;
      color: var(--muted);
    }}
    .details {{
      display: grid;
      gap: 10px;
      margin-top: 22px;
      padding-top: 18px;
      border-top: 1px solid var(--border);
    }}
    .detail {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      color: var(--muted);
      font-size: 14px;
    }}
    .detail strong {{
      color: var(--text);
      font-weight: 650;
      overflow-wrap: anywhere;
      text-align: right;
    }}
    .reason {{
      margin: 0;
      color: var(--text);
    }}
    .button {{
      display: inline-flex;
      justify-content: center;
      width: 100%;
      margin-top: 26px;
      padding: 12px 16px;
      border-radius: 8px;
      background: var(--accent);
      color: #06110a;
      font-weight: 700;
      text-decoration: none;
    }}
    .hint {{
      margin-top: 18px;
      font-size: 14px;
    }}
    @media (max-width: 480px) {{
      main {{ padding: 22px; }}
      h1 {{ font-size: 24px; }}
      .detail {{
        display: grid;
        gap: 3px;
      }}
      .detail strong {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <main>
    <p class="brand">Nano GitHub</p>
    <div class="status"><span class="dot" aria-hidden="true"></span>{safe_title}</div>
    <h1>{safe_message}</h1>
    <div class="details">
      {details}
    </div>
    <p class="hint">{hint}</p>
    <a class="button" href="https://discord.com/channels/@me">Return to Discord</a>
  </main>
</body>
</html>"""
