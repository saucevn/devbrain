"""Fail-closed guards for the webhook receiver (audit P0-1, L-2, L-3).

An empty GITHUB_WEBHOOK_SECRET must NEVER be used for HMAC verification —
hmac.new(b"", body) is computable by any attacker → event-store poisoning.
"""
import pytest
from fastapi import HTTPException

import pipeline


def test_guards_reject_empty_secret_with_503():
    with pytest.raises(HTTPException) as ei:
        pipeline.check_webhook_guards(b"", "delivery-1", "100")
    assert ei.value.status_code == 503


def test_guards_reject_missing_delivery_id_with_400():
    # empty string would become dedup key ('github','') and permanently claim it
    with pytest.raises(HTTPException) as ei:
        pipeline.check_webhook_guards(b"secret", "", "100")
    assert ei.value.status_code == 400


def test_guards_reject_oversize_body_with_413():
    with pytest.raises(HTTPException) as ei:
        pipeline.check_webhook_guards(b"secret", "d-1", str(pipeline.MAX_BODY_BYTES + 1))
    assert ei.value.status_code == 413


def test_guards_pass_normal_request():
    pipeline.check_webhook_guards(b"secret", "d-1", "12345")
    pipeline.check_webhook_guards(b"secret", "d-1", None)          # no content-length header
    pipeline.check_webhook_guards(b"secret", "d-1", "not-a-number")  # garbage header ignored


# ---- /health must speak status codes (probes don't parse bodies) ----------
def test_health_payload_ok():
    assert pipeline.health_payload(True) == (200, {"ok": True, "db": True})


def test_health_payload_db_down_is_503():
    code, body = pipeline.health_payload(False)
    assert code == 503
    assert body == {"ok": False, "db": False}


def test_fastapi_docs_disabled():
    # H-5: swagger/openapi must not be served through the Cloudflare tunnel
    assert pipeline.app.docs_url is None
    assert pipeline.app.redoc_url is None
    assert pipeline.app.openapi_url is None
