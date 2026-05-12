import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestAgentWrapper:
    def test_module_imports(self):
        from scripts.agent_wrapper import ask_agent
        assert callable(ask_agent)

    def test_find_binary_raises(self):
        from scripts.agent_wrapper import _find_binary
        with pytest.raises(FileNotFoundError):
            _find_binary("nonexistent_xyz", ["/tmp/nonexistent_xyz"])

    def test_extract_code_block(self):
        from scripts.agent_wrapper import _extract_json_block
        text = 'Root cause: missing fi\n```json\n{"confidence": 0.9, "root_cause": "test"}\n```'
        result = _extract_json_block(text)
        assert result is not None
        assert "confidence" in result

    def test_extract_naked_json(self):
        from scripts.agent_wrapper import _extract_json_block
        text = 'Diagnosis: {"confidence": 0.85, "error_type": "config"}'
        result = _extract_json_block(text)
        assert result is not None
        assert "0.85" in result

    def test_extract_no_json_returns_none(self):
        from scripts.agent_wrapper import _extract_json_block
        result = _extract_json_block("Just plain text, no JSON here.")
        assert result is None

    def test_get_agent_env(self):
        from scripts.agent_wrapper import _get_agent_env
        env = _get_agent_env()
        assert "HOME" in env
        assert "PATH" in env
