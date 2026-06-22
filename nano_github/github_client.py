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


@dataclass(frozen=True)
class SubmittedPullRequestReview:
    owner: str
    repo: str
    pull_number: int
    review_id: int
    url: str | None
    state: str | None


@dataclass(frozen=True)
class RepositoryPermissionCheck:
    owner: str
    repo: str
    installed: bool
    installation_id: int | None
    issues: str | None
    pull_requests: str | None
    can_create_issues: bool
    can_review_pull_requests: bool


@dataclass(frozen=True)
class _InstallationAccess:
    token: str
    permissions: dict[str, str]


class GitHubClientError(Exception):
    user_message = "GitHub API request failed."


class GitHubAppNotConfigured(GitHubClientError):
    user_message = "GitHub App authentication is not configured."


class GitHubAppNotInstalled(GitHubClientError):
    user_message = "GitHub App is not installed for this repository."


class GitHubAppMissingPermission(GitHubClientError):
    def __init__(self, permission: str, action: str) -> None:
        self.permission = permission
        self.action = action
        self.user_message = (
            f"Nano GitHub does not currently have {action} permissions for this repository."
        )
        super().__init__(self.user_message)


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

    access = _create_installation_access(installation.id)
    _require_permission(
        access.permissions,
        "issues",
        "write",
        "Issue creation",
    )
    issue = _request_json(
        "POST",
        f"/repos/{_quote(owner)}/{_quote(repo)}/issues",
        token=access.token,
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
                token=access.token,
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


def submit_pull_request_review(
    owner: str,
    repo: str,
    pull_number: int,
    event: str,
    body: str | None = None,
) -> SubmittedPullRequestReview:
    owner, repo = _normalize_repo(owner, repo)
    event = event.strip().upper()
    if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
        raise ValueError(f"Unsupported pull request review event: {event}")

    installation = get_installation_for_repo(owner, repo)
    if installation is None:
        raise GitHubAppNotInstalled()

    access = _create_installation_access(installation.id)
    _require_permission(
        access.permissions,
        "pull_requests",
        "write",
        "Pull Request Review",
    )

    payload: dict[str, Any] = {"event": event}
    if body:
        payload["body"] = body
    elif event in {"REQUEST_CHANGES", "COMMENT"}:
        payload["body"] = f"{event.replace('_', ' ').title()} from Nano GitHub."

    review = _request_json(
        "POST",
        f"/repos/{_quote(owner)}/{_quote(repo)}/pulls/{pull_number}/reviews",
        token=access.token,
        payload=payload,
    )

    review_id = review.get("id")
    if not isinstance(review_id, int):
        raise GitHubAPIError(502, "GitHub pull request review response was missing an id.")

    url = review.get("html_url")
    state = review.get("state")
    return SubmittedPullRequestReview(
        owner=owner,
        repo=repo,
        pull_number=pull_number,
        review_id=review_id,
        url=url if isinstance(url, str) else None,
        state=state if isinstance(state, str) else None,
    )


def check_repository_permissions(owner: str, repo: str) -> RepositoryPermissionCheck:
    owner, repo = _normalize_repo(owner, repo)
    installation = get_installation_for_repo(owner, repo)
    if installation is None:
        return RepositoryPermissionCheck(
            owner=owner,
            repo=repo,
            installed=False,
            installation_id=None,
            issues=None,
            pull_requests=None,
            can_create_issues=False,
            can_review_pull_requests=False,
        )

    access = _create_installation_access(installation.id)
    issues = access.permissions.get("issues")
    pull_requests = access.permissions.get("pull_requests")
    return RepositoryPermissionCheck(
        owner=owner,
        repo=repo,
        installed=True,
        installation_id=installation.id,
        issues=issues,
        pull_requests=pull_requests,
        can_create_issues=_permission_at_least(issues, "write"),
        can_review_pull_requests=_permission_at_least(pull_requests, "write"),
    )


def _create_installation_token(installation_id: int) -> str:
    return _create_installation_access(installation_id).token


def _create_installation_access(installation_id: int) -> _InstallationAccess:
    data = _request_json(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        app_auth=True,
    )
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise GitHubAPIError(502, "GitHub installation token response was missing a token.")
    permissions = data.get("permissions")
    normalized_permissions = {
        key: value
        for key, value in (permissions or {}).items()
        if isinstance(key, str) and isinstance(value, str)
    } if isinstance(permissions, dict) else {}
    return _InstallationAccess(token=token, permissions=normalized_permissions)


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


def _require_permission(
    permissions: dict[str, str],
    permission: str,
    required_level: str,
    action: str,
) -> None:
    if not _permission_at_least(permissions.get(permission), required_level):
        raise GitHubAppMissingPermission(permission, action)


def _permission_at_least(actual: str | None, required: str) -> bool:
    levels = {"read": 1, "write": 2}
    actual_level = levels.get(actual or "", 0)
    required_level = levels.get(required, 0)
    return actual_level >= required_level


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
