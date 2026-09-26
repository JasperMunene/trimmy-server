from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional


class Settings(BaseSettings):
    PROJECT_NAME: str = "Trimmy API"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"

    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379/0"
    RABBITMQ_URL: str = "amqp://guest:guest@localhost:5672/"

    # File storage for uploads (business photos, ...).
    #   r2:    Cloudflare R2 (any S3-compatible store works); used when R2_BUCKET is set
    #   local: files under MEDIA_ROOT, served by the API at MEDIA_BASE_URL (/media); dev only
    STORAGE_BACKEND: str = ""  # empty = r2 if R2_BUCKET is set, else local
    R2_ENDPOINT: str = ""  # https://<account_id>.r2.cloudflarestorage.com
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET: str = ""
    # Public base URL of the bucket (r2.dev subdomain or custom domain), used in photo URLs.
    R2_PUBLIC_URL: str = ""
    MEDIA_ROOT: str = "media"
    MEDIA_BASE_URL: str = "http://localhost:8000/media"
    MAX_PHOTO_BYTES: int = 10 * 1024 * 1024

    # How long an unpaid booking (pending_payment) holds its slot (docs/booking/PLAN.md, D6).
    BOOKING_HOLD_MINUTES: int = 10
    # How long a presigned upload URL stays valid.
    UPLOAD_URL_EXPIRE_SECONDS: int = 600
    WORKER_CONCURRENCY: int = 10

    CORS_ORIGINS: List[str] = ["http://localhost:8081", "http://localhost:19006"]

    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = "noreply@yourdomain.com"
    CLIENT_URL: str = "http://localhost:8081"

    # M-Pesa Daraja (STK push). Trimmy's own app and shortcode sign every push;
    # the money is routed to each business's payment destination (PartyB).
    MPESA_ENV: str = "sandbox"  # sandbox | production
    MPESA_CONSUMER_KEY: str = ""
    MPESA_CONSUMER_SECRET: str = ""
    MPESA_SHORTCODE: str = ""
    MPESA_PASSKEY: str = ""
    # Public https URL of POST /payments/mpesa/callback; the secret is appended as
    # the last path segment so only Daraja (who got the full URL) can call it.
    MPESA_CALLBACK_URL: str = ""
    MPESA_CALLBACK_SECRET: str = ""

    # SMS (HostPinnacle / Intacom gateway) for phone OTP delivery.
    SMS_API_KEY: str = ""
    SMS_USER_ID: str = ""
    SMS_PASSWORD: str = ""
    SMS_SENDER_ID: str = "Intacom"
    # Region assumed when a phone number has no international prefix.
    SMS_DEFAULT_COUNTRY: str = "KE"

    GOOGLE_AUTH_CLIENT_ID: str = ""
    GOOGLE_AUTH_CLIENT_SECRET: str = ""
    # This server's callback. Must be listed exactly under "Authorized redirect URIs"
    # for the OAuth client in Google Cloud Console.
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/google/authorized"
    # Where the callback sends the user afterwards, per platform.
    GOOGLE_WEB_REDIRECT_URL: str = "http://localhost:8081/auth/google"
    GOOGLE_MOBILE_REDIRECT_URL: str = "trimmy://auth/google"

    RATE_LIMIT_LOGIN: str = "5/minute"
    RATE_LIMIT_REGISTER: str = "3/minute"
    RATE_LIMIT_CHECK_EMAIL: str = "10/minute"

    OTP_MAX_ATTEMPTS: int = 5
    OTP_RESEND_COOLDOWN_SECONDS: int = 60
    OTP_MAX_SENDS_PER_HOUR: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
