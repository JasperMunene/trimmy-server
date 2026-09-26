from app.core.config import settings
from tests.conftest import EXTERNAL_SEND_SETTINGS


def test_tests_cannot_reach_outside_services():
    """If this fails, a test run could send real emails, SMS or M-Pesa prompts."""
    leaked = [name for name in EXTERNAL_SEND_SETTINGS if getattr(settings, name, "")]
    assert leaked == []
    assert settings.STORAGE_BACKEND == "local"
