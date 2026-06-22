from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nano_github.config import settings


@dataclass(frozen=True)
class GitHubInstallation:
    id: int
    account_login: str | None = None


@dataclass(frozen=True)
class CreatedIssue:
    owner: str
    repo: str
    number: int
    title: str
    url: str
    labels_applied: bool
    label_error: str | None = None


class GitHubClientError(Exception):
    user_message = "GitHub API request failed."


class GitHubAppNotConfigured(GitHubClientError):
    user_message = "GitHub App authentication is not configured."


class GitHubAppNotInstalled(GitHubClientError):
    user_message = "GitHub App is not installed for this repository."


class GitHubAPIError(GitHubClientError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"GitHub API returned {status_code}: {message}")


def get_installation_for_repo(owner: str, repo: str) -> GitHubInstallation | None:
    owner, repo = _normalize_repo(owner, repo)
    try:
        data = _request_json(
            "GET",
            f"/repos/{_quote(owner)}/{_quote(repo)}/installation",
            app_auth=True,
        )
    except GitHubAPIError as exc:
        if exc.status_code == 404:
            return None
        raise

    installation_id = data.get("id")
    if not isinstance(installation_id, int):
        raise GitHubAPIError(502, "GitHub installation response did not include an id.")

    account = data.get("account") or {}
    account_login = account.get("login") if isinstance(account, dict) else None
    return GitHubInstallation(
        id=installation_id,
        account_login=account_login if isinstance(account_login, str) else None,
    )


def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> CreatedIssue:
    owner, repo = _normalize_repo(owner, repo)
    labels = [label.strip() for label in labels or [] if label.strip()]

    installation = get_installation_for_repo(owner, repo)
    if installation is None:
        raise GitHubAppNotInstalled()

    token = _create_installation_token(installation.id)
    issue = _request_json(
        "POST",
        f"/repos/{_quote(owner)}/{_quote(repo)}/issues",
        token=token,
        payload={"title": title, "body": body},
    )

    number = issue.get("number")
    url = issue.get("html_url")
    created_title = issue.get("title")
    if not isinstance(number, int) or not isinstance(url, str):
        raise GitHubAPIError(502, "GitHub issue response was missing issue details.")

    labels_applied = not labels
    label_error: str | None = None
    if labels:
        try:
            _request_json(
                "POST",
                f"/repos/{_quote(owner)}/{_quote(repo)}/issues/{number}/labels",
                token=token,
                payload={"labels": labels},
            )
            labels_applied = True
        except GitHubAPIError as exc:
            labels_applied = False
            label_error = f"GitHub rejected issue labels ({exc.status_code})."

    return CreatedIssue(
        owner=owner,
        repo=repo,
        number=number,
        title=created_title if isinstance(created_title, str) else title,
        url=url,
        labels_applied=labels_applied,
        label_error=label_error,
    )


def _create_installation_token(installation_id: int) -> str:
    data = _request_json(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        app_auth=True,
    )
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise GitHubAPIError(502, "GitHub installation token response was missing a token.")
    return token


def _request_json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    app_auth: bool = False,
) -> dict[str, Any]:
    url = _api_url(path)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Nano-GitHub",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if app_auth:
        headers["Authorization"] = f"Bearer {_github_app_jwt()}"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        raise GitHubAppNotConfigured()

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        message = _error_message(exc)
        raise GitHubAPIError(exc.code, message) from exc
    except urllib.error.URLError as exc:
        raise GitHubAPIError(503, str(exc.reason)) from exc

    if not response_body:
        return {}

    try:
        data = json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GitHubAPIError(502, "GitHub returned malformed JSON.") from exc

    if not isinstance(data, dict):
        raise GitHubAPIError(502, "GitHub returned an unexpected response.")
    return data


def _github_app_jwt() -> str:
    app_id = settings.github_app_id
    private_key = _private_key()
    if not app_id or not private_key:
        raise GitHubAppNotConfigured()

    try:
        import jwt
    except ImportError as exc:
        raise GitHubAppNotConfigured() from exc

    now = int(time.time())
    encoded = jwt.encode(
        {
            "iat": now - 60,
            "exp": now + 540,
            "iss": app_id,
        },
        private_key,
        algorithm="RS256",
    )
    return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded


def _private_key() -> str | None:
    if settings.github_app_private_key:
        return settings.github_app_private_key.replace("\\n", "\n")
    if settings.github_app_private_key_path:
        return Path(settings.github_app_private_key_path).read_text(encoding="utf-8")
    return None


def _api_url(path: str) -> str:
    base_url = settings.github_api_base_url.rstrip("/")
    return f"{base_url}/{path.lstrip('/')}"


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _normalize_repo(owner: str, repo: str) -> tuple[str, str]:
    return owner.strip().lower(), repo.strip().lower()


def _error_message(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8")
    except OSError:
        return exc.reason

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return exc.reason

    message = data.get("message") if isinstance(data, dict) else None
    return message if isinstance(message, str) and message else exc.reason
