"""Make an existing Trimmy account an admin (the first one; later ones from the admin app's Team page).

    python -m app.scripts.make_admin someone@example.com "Founding team"

They sign in to the admin app with that account's email and password and set up an authenticator
app on the first sign-in. --revoke removes admin access.
"""
import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.core import audit
from app.core.database import AsyncSessionLocal
from app.modules.auth.models import RefreshToken, User


async def main(email: str, reason: str, revoke: bool) -> int:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(func.lower(User.email) == email.strip().lower()))).scalar_one_or_none()
        if user is None:
            print(f"No account with email {email}. Sign up in the app first.", file=sys.stderr)
            return 1
        if revoke:
            from sqlalchemy import delete

            user.platform_role = None
            user.totp_secret = user.totp_enabled_at = user.totp_last_step = None
            await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
            audit.record(db, actor=None, action="admin.revoke", target_type="user", target_id=user.id,
                         reason=f"CLI: {reason}")
        else:
            if not user.password_hash:
                print("That account has no password (Google / phone sign-in). Set one first.", file=sys.stderr)
                return 1
            user.platform_role = "admin"
            audit.record(db, actor=None, action="admin.grant", target_type="user", target_id=user.id,
                         reason=f"CLI: {reason}")
        await db.commit()
        print(f"{email}: {'no longer an admin' if revoke else 'admin'}")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email")
    parser.add_argument("reason", nargs="?", default="Set up from the command line")
    parser.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.email, args.reason, args.revoke)))
