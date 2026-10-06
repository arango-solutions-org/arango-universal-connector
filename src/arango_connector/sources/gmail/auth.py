"""Gmail credentials: service account with domain-wide delegation, or per-mailbox OAuth.

Only the read-only scope is ever requested; the connector cannot send, modify or delete
mail.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from arango_connector.sources.gmail.config import AuthMode

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def token_path(token_dir: Path, mailbox: str) -> Path:
    return token_dir / f"{mailbox.lower()}.json"


def build_credentials(
    mode: AuthMode,
    mailbox: str,
    *,
    service_account_file: Path | None = None,
    token_dir: Path | None = None,
) -> Any:
    """Credentials to read ``mailbox``. Refreshing access tokens is handled by google-auth."""
    if mode == "service_account":
        from google.oauth2 import service_account

        if service_account_file is None:
            raise ValueError("service_account mode needs a service account file")
        # with_subject impersonates the mailbox via domain-wide delegation.
        return service_account.Credentials.from_service_account_file(
            str(service_account_file), scopes=SCOPES, subject=mailbox
        )

    from google.oauth2.credentials import Credentials

    if token_dir is None:
        raise ValueError("oauth mode needs a token directory")
    return Credentials.from_authorized_user_file(str(token_path(token_dir, mailbox)), SCOPES)


def auth_main(argv: list[str] | None = None) -> int:
    """``arango-connector-gmail-auth``: authorise one mailbox and save its token file.

    Opens a browser for the Google consent screen (installed-app flow) with the
    read-only scope, checks the signed-in account is the requested mailbox, and writes
    ``<out-dir>/<mailbox>.json`` readable only by the current user.
    """
    parser = argparse.ArgumentParser(
        prog="arango-connector-gmail-auth",
        description="Authorise read-only Gmail access for one mailbox (OAuth mode).",
    )
    parser.add_argument(
        "--client-secrets",
        type=Path,
        required=True,
        help="OAuth client JSON (Desktop app) from Google Cloud Console",
    )
    parser.add_argument("--mailbox", required=True, help="address to authorise")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("./secrets/gmail"),
        help="where to write the token file (GMAIL_OAUTH_TOKEN_DIR)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the consent URL instead of opening a browser",
    )
    args = parser.parse_args(argv)

    from google_auth_oauthlib.flow import InstalledAppFlow

    from arango_connector.sources.gmail.client import GoogleGmailClient

    mailbox = args.mailbox.lower()
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets), SCOPES)
    credentials = flow.run_local_server(
        port=0, login_hint=mailbox, open_browser=not args.no_browser
    )

    signed_in = str(GoogleGmailClient(credentials).get_profile().get("emailAddress", ""))
    if signed_in.lower() != mailbox:
        print(f"Signed in as {signed_in!r}, not {mailbox!r}; token not saved.", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = token_path(args.out_dir, mailbox)
    # Created 0600 so the refresh token is never world-readable, even briefly.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # an existing file keeps its old mode otherwise
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(credentials.to_json())
    print(f"Saved token for {mailbox} to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(auth_main())
