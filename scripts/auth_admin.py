#!/usr/bin/env python3
"""Local account bootstrap and emergency administration.

Temporary passwords and bearer tokens are printed exactly once. Run this from
the repository root under the same Unix account as the service.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth import AuthError, auth_repository, initialize_auth


def _temporary_password() -> str:
    return secrets.token_urlsafe(18)


def _safe_user(user: dict) -> dict:
    return {
        key: user.get(key)
        for key in (
            "id",
            "username",
            "is_admin",
            "is_active",
            "must_change_password",
            "last_login_at",
            "created_at",
        )
    }


def _print_secret(label: str, username: str, secret: str) -> None:
    print(f"username: {username}")
    print(f"{label}: {secret}")
    print("This secret is shown once; store and deliver it securely.")


def init_admin(args: argparse.Namespace) -> int:
    if auth_repository.list_users():
        print("Refusing to create a second legacy-data owner: accounts already exist.", file=sys.stderr)
        return 2
    password = _temporary_password()
    user = auth_repository.create_user(
        args.username,
        password,
        is_admin=True,
        must_change_password=True,
        use_legacy_data=True,
    )
    _print_secret("temporary_password", user["username"], password)
    return 0


def create_user(args: argparse.Namespace) -> int:
    password = _temporary_password()
    user = auth_repository.create_user(
        args.username,
        password,
        is_admin=bool(args.admin),
        must_change_password=True,
        use_legacy_data=False,
    )
    _print_secret("temporary_password", user["username"], password)
    return 0


def list_users(_args: argparse.Namespace) -> int:
    print(json.dumps(
        [_safe_user(user) for user in auth_repository.list_users()],
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def reset_password(args: argparse.Namespace) -> int:
    user = auth_repository.get_user_by_username(args.username)
    password = _temporary_password()
    auth_repository.set_password(
        user["id"], password, must_change_password=True
    )
    _print_secret("temporary_password", user["username"], password)
    return 0


def create_token(args: argparse.Namespace) -> int:
    token, _metadata = auth_repository.create_api_token(
        args.username,
        args.name,
        expires_days=args.expires_days,
    )
    _print_secret("bearer_token", args.username, token)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Memory account administration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser(
        "init-admin", help="create the first admin and attach existing data"
    )
    init.add_argument("--username", default="admin")
    init.set_defaults(handler=init_admin)

    create = subparsers.add_parser(
        "create-user", help="create a user with an empty isolated data space"
    )
    create.add_argument("--username", required=True)
    create.add_argument("--admin", action="store_true")
    create.set_defaults(handler=create_user)

    listing = subparsers.add_parser("list-users", help="list account metadata")
    listing.set_defaults(handler=list_users)

    reset = subparsers.add_parser(
        "reset-password", help="revoke sessions/tokens and issue a temporary password"
    )
    reset.add_argument("--username", required=True)
    reset.set_defaults(handler=reset_password)

    token = subparsers.add_parser(
        "create-token", help="issue a bearer token for CLI or MCP access"
    )
    token.add_argument("--username", required=True)
    token.add_argument("--name", required=True)
    token.add_argument("--expires-days", type=int)
    token.set_defaults(handler=create_token)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    initialize_auth()
    try:
        return int(args.handler(args))
    except AuthError as exc:
        print(f"{exc.code}: {exc.detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
