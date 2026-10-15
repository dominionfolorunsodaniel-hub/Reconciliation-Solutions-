"""
manage.py - admin tool for when you're locked out or setting things up from the server.

    python manage.py create-admin NAME     create an admin (asks for a password)
    python manage.py reset-password NAME   set a new password
    python manage.py reset-2fa NAME        switch off two-factor for a user who lost their phone
    python manage.py list-users
    python manage.py backup                make a backup now
"""
import argparse
import getpass
import sys

import db


def _ask_password() -> str:
    pw = getpass.getpass("Password (10+ characters): ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords don't match.")
    return pw


def _user(name: str) -> dict:
    user = db.get_user_by_name(name)
    if not user:
        sys.exit(f"No user named {name!r}.")
    return user


def main() -> None:
    p = argparse.ArgumentParser(description="Report Analyzer admin tool")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("create-admin", "reset-password", "reset-2fa"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("list-users")
    sub.add_parser("backup")
    args = p.parse_args()

    if args.cmd == "create-admin":
        db.create_user(args.username, _ask_password(), "admin")
        print(f"Admin {args.username!r} created.")
    elif args.cmd == "reset-password":
        db.update_user(_user(args.username)["id"], password=_ask_password())
        print("Password changed (all sessions signed out).")
    elif args.cmd == "reset-2fa":
        db.update_user(_user(args.username)["id"], reset_2fa=True)
        print("Two-factor switched off for that user; they can set it up again after logging in.")
    elif args.cmd == "list-users":
        for u in db.list_users():
            print(f"{u['username']:<20} {u['role']:<8} active={u['active']} 2fa={u['totp_enabled']} telegram={u['telegram_id']}")
    elif args.cmd == "backup":
        import backup

        print("Backup written:", backup.create_backup())


if __name__ == "__main__":
    main()
