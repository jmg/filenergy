"""Run with `flask --app manage run` or `python manage.py`.

Usage:
  python manage.py                      # start the dev server
  python manage.py reindex              # re-extract + re-embed every file
  python manage.py create-superuser EMAIL PASSWORD
  python manage.py generate-encryption-key
  python manage.py reencrypt            # back-fill at-rest encryption
"""
from __future__ import annotations

import sys

from filenergy import app, db
from filenergy.models import File, User
from filenergy.services.file import FileService


def cmd_reindex():
    with app.app_context():
        files = File.query.all()
        svc = FileService()
        for f in files:
            f.indexed_at = None
            f.index_error = None
            db.session.commit()
            svc.index_file(f)
            print(f"reindexed: {f.name} ({'ok' if f.indexed_at else f.index_error})")


def cmd_create_superuser(email: str, password: str):
    with app.app_context():
        if User.query.filter_by(email=email).first():
            print("User already exists")
            return
        user = User(email=email, username=email, is_superuser=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f"created superuser: {email}")


def cmd_generate_encryption_key():
    from filenergy.services import crypto
    print(crypto.generate_key())


def cmd_reencrypt():
    from filenergy.services import crypto
    with app.app_context():
        if not crypto.is_configured():
            print("FILENERGY_ENCRYPTION_KEY is not set; nothing to do.")
            return
        counts = crypto.reencrypt_all()
        print(f"re-encrypted: {counts}")


def main(argv: list[str]):
    if len(argv) < 2:
        app.run(debug=True, host="0.0.0.0", port=5000)
        return
    cmd, *rest = argv[1:]
    if cmd == "reindex":
        cmd_reindex()
    elif cmd == "create-superuser":
        cmd_create_superuser(*rest)
    elif cmd == "generate-encryption-key":
        cmd_generate_encryption_key()
    elif cmd == "reencrypt":
        cmd_reencrypt()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
