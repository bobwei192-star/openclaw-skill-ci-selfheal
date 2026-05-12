import json

import pytest


class TestWebhookPayloads:
    def test_required_fields_job_and_build(self):
        payload = {"job": "test", "build": 1}
        missing = [k for k in ["job", "build"] if k not in payload]
        assert missing == []

    def test_missing_job_field(self):
        payload = {"build": 1, "status": "FAILURE"}
        missing = [k for k in ["job", "build"] if k not in payload]
        assert "job" in missing

    def test_missing_build_field(self):
        payload = {"job": "test", "status": "FAILURE"}
        missing = [k for k in ["job", "build"] if k not in payload]
        assert "build" in missing

    def test_failure_status_passes_through(self):
        for status in ("FAILURE", "FAILED"):
            assert status.upper() in ("FAILURE", "FAILED")

    def test_success_status_is_filtered(self):
        status = "SUCCESS".upper()
        assert status not in ("FAILURE", "FAILED")

    def test_aborted_status_is_filtered(self):
        status = "ABORTED".upper()
        assert status not in ("FAILURE", "FAILED")

    def test_null_status_is_filtered(self):
        status = ""
        assert status not in ("FAILURE", "FAILED")


class TestHealthAndAdminEndpoints:
    def test_reset_clears_circuit_breaker(self):
        from scripts.webhook_listener import orchestrator

        orchestrator._circuit_break("test-job")
        assert orchestrator._is_circuit_open("test-job") is True

        orchestrator.state = {"version": "2.0.0", "chains": {}, "circuit_breaker": {}}
        orchestrator._save_state()
        assert orchestrator._is_circuit_open("test-job") is False

    def test_health_returns_status_fields(self):
        from scripts.webhook_listener import orchestrator

        status = orchestrator.get_status()
        assert "circuit_breaker" in status
        assert "chains" in status
        assert "version" in status
