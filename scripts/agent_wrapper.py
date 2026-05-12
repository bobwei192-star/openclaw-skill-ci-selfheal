import json
import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

_OPENCLAW_BIN = None


def _find_binary(name, candidates):
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    try:
        r = subprocess.run(
            ["which", name], capture_output=True, text=True, timeout=5,
            env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")},
        )
        p = r.stdout.strip()
        if p:
            return p
    except Exception:
        pass
    raise FileNotFoundError(f"Cannot find '{name}'. Tried: {candidates}")


def _get_openclaw_bin():
    global _OPENCLAW_BIN
    if _OPENCLAW_BIN is None:
        _OPENCLAW_BIN = _find_binary("openclaw", ["/usr/local/bin/openclaw"])
    return _OPENCLAW_BIN


def _get_agent_env():
    return {
        **os.environ,
        "HOME": os.environ.get("HOME", "/home/node"),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "NODE_TLS_REJECT_UNAUTHORIZED": "0",
    }


def ask_agent(instruction: str, expect_json: bool = True, timeout: int = 180):
    cmd = [_get_openclaw_bin(), "agent", "--agent", "main", "--message", instruction]
    if expect_json:
        cmd.append("--json")

    logger.info("Agent instruction: %s...", instruction[:200])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_get_agent_env())
        stdout = result.stdout.strip()
        if not stdout:
            raise RuntimeError(
                f"Agent empty response (exit={result.returncode}) stderr: {result.stderr[:500]}"
            )

        if not expect_json:
            return stdout

        # ── 兜底调试 ──
        debug_path = "/tmp/agent_debug_raw.json"
        with open(debug_path, "w") as f:
            f.write(stdout)

        # ── 解析 Agent 响应信封 ──
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            logger.warning("Agent envelope is not valid JSON, raw saved to %s", debug_path)
            return {}

        # 提取 AI 实际回复文本
        ai_text = ""
        if isinstance(envelope, dict):
            for container in (envelope, envelope.get("result", {}), envelope.get("data", {})):
                payloads = container.get("payloads")
                if isinstance(payloads, list) and len(payloads) > 0:
                    ai_text = payloads[0].get("text", "")
                    break
            if not ai_text and isinstance(envelope.get("text"), str):
                ai_text = envelope["text"]
        elif isinstance(envelope, str):
            ai_text = envelope
        else:
            ai_text = stdout

        # ── 从 AI 文本中提取 JSON ──
        json_str = _extract_json_block(ai_text)
        if json_str:
            return json.loads(json_str)

        logger.warning("No JSON block found in agent response. Text[:500]: %s", ai_text[:500])
        return {}

    except subprocess.TimeoutExpired:
        logger.error("Agent call timed out after %ds", timeout)
        raise


def _extract_json_block(text):
    """从 AI 回复中提取 JSON：优先 ```json ... ```，其次裸 { ... }"""
    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if code_block:
        candidate = code_block.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        candidate = text[start:end]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return None
