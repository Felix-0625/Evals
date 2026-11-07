"""Malformed experience fields must fail before touching workspace state."""

from datetime import UTC, datetime, timedelta

import pytest

from evals_app.monitoring import experience


@pytest.mark.parametrize(
    "field,value",
    [("expires_at", 7), ("enabled", "false"), ("enabled", 1), ("text", ["rule"]), ("id", " ")],
)
def test_experience_rejects_invalid_field_types(field, value):
    content = {
        "id": "rule",
        "version": "1",
        "text": "Verify the result.",
        "application_id": "app",
        "source": "review",
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "enabled": False,
        field: value,
    }
    with pytest.raises(ValueError, match="Experience requires"):
        experience(None, content, key="invalid")
