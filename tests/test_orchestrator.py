import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.orchestrator import Orchestrator


_CONFIG_TEMPLATE = """
jenkins:
  url_env: "JENKINS_URL"
  user_env: "JENKINS_USER"
  token_env: "JENKINS_API_TOKEN"
gitlab:
  url: "http://test:8441"
  token_env: "TEST_TOKEN"
repair:
  max_retries: {max_retries}
  poll_interval_sec: 1
  build_timeout_min: 1
whitelist:
  repos: {repos}
  branch_pattern: "{branch_pattern}"
  protected_branches: {protected}
notify:
  dingtalk_webhook_env: ""
"""


def _make_config(tmp_path, max_retries=3, repos=None, branch_pattern=".*", protected=None):
    config = tmp_path / "config.yaml"
    config.write_text(_CONFIG_TEMPLATE.format(
        max_retries=max_retries,
        repos=json.dumps(repos or []),
        branch_pattern=branch_pattern,
        protected=json.dumps(protected or []),
    ))
    return Orchestrator(config_path=str(config))


class TestWhitelistCheck:
    @pytest.fixture
    def orchestrator(self, tmp_path):
        return _make_config(
            tmp_path, max_retries=3,
            repos=["root/model_test", "group/backend-api"],
            branch_pattern="^(feat|fix|dev|feature)(/.*)?$",
            protected=["main", "master", "release/*"],
        )

    def test_whitelisted_repo_and_branch(self, orchestrator):
        assert orchestrator._whitelist_check("root/model_test", "dev/test") is True

    def test_whitelisted_repo_simple_branch(self, orchestrator):
        assert orchestrator._whitelist_check("group/backend-api", "feat") is True

    def test_protected_branch_main(self, orchestrator):
        assert orchestrator._whitelist_check("root/model_test", "main") is False

    def test_protected_branch_master(self, orchestrator):
        assert orchestrator._whitelist_check("root/model_test", "master") is False

    def test_protected_branch_release(self, orchestrator):
        assert orchestrator._whitelist_check("root/model_test", "release/v1.0") is False

    def test_unknown_repo(self, orchestrator):
        assert orchestrator._whitelist_check("unknown/repo", "dev/test") is False

    def test_branch_not_matching_pattern(self, orchestrator):
        assert orchestrator._whitelist_check("root/model_test", "hotfix/urgent") is False


class TestDesensitize:
    @pytest.fixture
    def orchestrator(self, tmp_path):
        return _make_config(tmp_path, max_retries=3)

    def test_token_is_redacted(self, orchestrator):
        text = "password=supersecret123"
        result = orchestrator._desensitize(text)
        assert "supersecret" not in result
        assert "[REDACTED]" in result

    def test_api_token_is_redacted(self, orchestrator):
        text = "JENKINS_API_TOKEN=abcdef123456"
        result = orchestrator._desensitize(text)
        assert "abcdef123456" not in result

    def test_internal_ip_is_redacted(self, orchestrator):
        result = orchestrator._desensitize("connect to 192.168.1.100")
        assert "[INTERNAL_IP]" in result
        assert "192.168.1.100" not in result

    def test_docker_ip_is_redacted(self, orchestrator):
        result = orchestrator._desensitize("host 172.19.0.5 unreachable")
        assert "[INTERNAL_IP]" in result

    def test_non_sensitive_unchanged(self, orchestrator):
        text = "Build step: make all"
        result = orchestrator._desensitize(text)
        assert result == text


class TestBackoff:
    @pytest.fixture
    def orchestrator(self, tmp_path):
        return _make_config(tmp_path, max_retries=5)

    def test_attempt_1(self, orchestrator):
        assert orchestrator._backoff(1) == 1

    def test_attempt_3(self, orchestrator):
        assert orchestrator._backoff(3) == 15

    def test_attempt_5(self, orchestrator):
        assert orchestrator._backoff(5) == 60

    def test_attempt_beyond_list(self, orchestrator):
        assert orchestrator._backoff(10) == 60


class TestStatePersistence:
    @pytest.fixture
    def orchestrator(self, tmp_path):
        import scripts.orchestrator as orch
        state_file = tmp_path / ".self-heal-state.json"
        orch.STATE_FILE = state_file
        return _make_config(tmp_path, max_retries=3)

    def test_initial_state_empty(self, orchestrator):
        status = orchestrator.get_status()
        assert status["version"] == "2.0.0"
        assert status["chains"] == {}
        assert status["circuit_breaker"] == {}

    def test_new_chain_created(self, orchestrator):
        chain = orchestrator._get_chain("test-job")
        assert chain["status"] == "idle"
        assert chain["current_retry"] == 0
        assert chain["processed_builds"] == []

    def test_duplicate_detection(self, orchestrator):
        assert orchestrator._is_duplicate("test-job", 1) is False
        assert orchestrator._is_duplicate("test-job", 1) is True

    def test_record_history(self, orchestrator):
        orchestrator._record_history("test-job", "SUCCESS", {"mr": "url"})
        chain = orchestrator._get_chain("test-job")
        assert chain["status"] == "SUCCESS"
        assert len(chain["history"]) == 1
        assert chain["history"][0]["status"] == "SUCCESS"

    def test_circuit_break_and_reset(self, orchestrator):
        orchestrator._circuit_break("test-job")
        assert orchestrator._is_circuit_open("test-job") is True
        orchestrator._reset_circuit("test-job")
        assert orchestrator._is_circuit_open("test-job") is False
