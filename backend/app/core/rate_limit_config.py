from dataclasses import dataclass
from enum import Enum


class RateLimitKey(Enum):
    IP = "ip"
    IP_ENDPOINT = "ip_endpoint"


@dataclass(frozen=True)
class RateLimitConfig:
    limit: int
    window_seconds: int
    block_seconds: int
    key_type: RateLimitKey


RATE_LIMITS: dict[str, RateLimitConfig] = {
    "register": RateLimitConfig(
        limit=5,
        window_seconds=60,
        block_seconds=3600,
        key_type=RateLimitKey.IP,
    ),
    "login": RateLimitConfig(
        limit=5,
        window_seconds=60,
        block_seconds=3600,
        key_type=RateLimitKey.IP,
    ),
    "resend_otp": RateLimitConfig(
        limit=3,
        window_seconds=60,
        block_seconds=600,
        key_type=RateLimitKey.IP,
    ),
    "forgot_password": RateLimitConfig(
        limit=5,
        window_seconds=3600,
        block_seconds=3600,
        key_type=RateLimitKey.IP,
    ),
    "verify_otp": RateLimitConfig(
        limit=5,
        window_seconds=60,
        block_seconds=600,
        key_type=RateLimitKey.IP,
    ),
}
