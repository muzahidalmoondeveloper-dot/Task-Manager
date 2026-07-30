import random
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_errors import AuthError
from app.models.auth_security import EmailOTP, IPAuthLock
from app.models.user import User
from app.services.email_service import EmailService


MAX_FAILED_ATTEMPTS = 3
LOCK_MINUTES = 3
OTP_EXPIRE_MINUTES = 10
LOGIN_OTP_VALID_DAYS = 7


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def generate_otp() -> str:
    return str(random.randint(100000, 999999))


def normalize_email(email: str) -> str:
    return email.lower().strip()


def normalize_otp(otp_code: str) -> str:
    return str(otp_code).strip()


class AuthSecurityService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.email_service = EmailService()

    async def check_ip_lock(self, ip_address: str) -> None:
        statement = select(IPAuthLock).where(IPAuthLock.ip_address == ip_address)
        result = await self.db.execute(statement)
        lock = result.scalar_one_or_none()

        if not lock or not lock.locked_until:
            return

        now = datetime.now(timezone.utc)
        locked_until = lock.locked_until
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)

        if locked_until > now:
            raise AuthError.rate_limited(
                f"Too many failed OTP attempts. Your IP is locked for {LOCK_MINUTES} minutes."
            )

        lock.failed_attempts = 0
        lock.locked_until = None
        await self.db.commit()

    async def record_failed_attempt(self, ip_address: str) -> None:
        now = datetime.now(timezone.utc)
        statement = select(IPAuthLock).where(IPAuthLock.ip_address == ip_address)
        result = await self.db.execute(statement)
        lock = result.scalar_one_or_none()

        if lock is None:
            lock = IPAuthLock(ip_address=ip_address, failed_attempts=1, last_failed_at=now)
            self.db.add(lock)
        else:
            lock.failed_attempts += 1
            lock.last_failed_at = now
            if lock.failed_attempts >= MAX_FAILED_ATTEMPTS:
                lock.locked_until = now + timedelta(minutes=LOCK_MINUTES)

        await self.db.commit()

    async def reset_failed_attempts(self, ip_address: str) -> None:
        statement = select(IPAuthLock).where(IPAuthLock.ip_address == ip_address)
        result = await self.db.execute(statement)
        lock = result.scalar_one_or_none()

        if lock:
            lock.failed_attempts = 0
            lock.locked_until = None
            await self.db.commit()

    async def create_and_send_otp(
        self,
        *,
        user: User | None,
        email: str,
        purpose: str,
    ) -> None:
        normalized_email = normalize_email(email)
        otp_code = generate_otp()

        await self.db.execute(
            update(EmailOTP)
            .where(EmailOTP.email == normalized_email)
            .where(EmailOTP.purpose == purpose)
            .where(EmailOTP.is_used.is_(False))
            .values(is_used=True)
        )

        otp = EmailOTP(
            user_id=user.id if user else None,
            email=normalized_email,
            otp_code=otp_code,
            purpose=purpose,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES),
            is_used=False,
        )

        self.db.add(otp)
        await self.db.commit()

        self.email_service.send_otp_email(
            to_email=normalized_email,
            otp_code=otp_code,
            purpose=purpose,
        )

    async def verify_otp(
        self,
        *,
        email: str,
        otp_code: str,
        purpose: str,
        ip_address: str,
    ) -> EmailOTP:
        await self.check_ip_lock(ip_address)

        now = datetime.now(timezone.utc)
        normalized_email = normalize_email(email)
        submitted_otp = normalize_otp(otp_code)

        statement = (
            select(EmailOTP)
            .where(EmailOTP.email == normalized_email)
            .where(EmailOTP.purpose == purpose)
            .where(EmailOTP.is_used.is_(False))
            .order_by(EmailOTP.created_at.desc())
        )

        result = await self.db.execute(statement)
        otp = result.scalars().first()

        if otp is None:
            await self.record_failed_attempt(ip_address)
            raise AuthError.invalid_otp()

        expires_at = otp.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at < now:
            otp.is_used = True
            await self.db.commit()
            await self.record_failed_attempt(ip_address)
            raise AuthError.otp_expired()

        if normalize_otp(otp.otp_code) != submitted_otp:
            await self.record_failed_attempt(ip_address)
            raise AuthError.invalid_otp()

        otp.is_used = True
        await self.db.commit()
        await self.reset_failed_attempts(ip_address)

        return otp

    def login_otp_required(self, user: User) -> bool:
        if user.last_login_otp_verified_at is None:
            return True

        verified_at = user.last_login_otp_verified_at
        if verified_at.tzinfo is None:
            verified_at = verified_at.replace(tzinfo=timezone.utc)

        return verified_at < datetime.now(timezone.utc) - timedelta(days=LOGIN_OTP_VALID_DAYS)
