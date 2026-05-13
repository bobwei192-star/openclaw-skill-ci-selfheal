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


def ask_agent(instruction: str, expect_json: bool = True, timeout: int = 180, skill_name: str = ""):
    cmd = [_get_openclaw_bin(), "agent", "--agent", "main", "--message", instruction]
    if expect_json:
        cmd.append("--json")

    skill_tag = f"skill={skill_name}" if skill_name else "skill=N/A"
    logger.info("[AGENT] ★ %s | 调用 OpenClaw 二进制: %s", skill_tag, _get_openclaw_bin())
    logger.info("[AGENT] ★ %s | 完整命令: %s --agent main --message '...' %s",
                 skill_tag, _get_openclaw_bin(), "--json" if expect_json else "")
    logger.info("[AGENT] ★ %s | 超时: %ss, expect_json=%s", skill_tag, timeout, expect_json)
    logger.info("[AGENT] ★ %s | 指令长度: %s 字符", skill_tag, len(instruction))
    logger.info("[AGENT] ★ %s | 环境: HOME=%s PATH=%s",
                 skill_tag,
                 os.environ.get("HOME", "N/A"),
                 os.environ.get("PATH", "N/A"))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_get_agent_env())
        stdout = result.stdout.strip()
        logger.info("[AGENT] ★ %s | 进程退出码: %s", skill_tag, result.returncode)
        logger.info("[AGENT] ★ %s | stdout: %s 字符, stderr: %s 字符",
                     skill_tag, len(result.stdout or ""), len(result.stderr or ""))
        if not stdout:
            logger.error("[AGENT] ★ %s | 返回空 (exit=%s) stderr[:500]: %s",
                          skill_tag, result.returncode, result.stderr[:500])
            raise RuntimeError(
                f"Agent empty response (exit={result.returncode}) stderr: {result.stderr[:500]}"
            )

        if not expect_json:
            logger.info("[AGENT] ★ %s | 纯文本模式，长度=%s", skill_tag, len(stdout))
            return stdout

        debug_path = "/tmp/agent_debug_raw.json"
        with open(debug_path, "w") as f:
            f.write(stdout)
        logger.info("[AGENT] ★ %s | 原始输出已保存: %s", skill_tag, debug_path)

        try:
            envelope = json.loads(stdout)
            logger.info("[AGENT] ★ %s | JSON 信封 keys=%s", skill_tag,
                         list(envelope.keys()) if isinstance(envelope, dict) else "not-dict")
        except json.JSONDecodeError:
            logger.warning("[AGENT] ★ %s | JSON 信封解析失败，已保存", skill_tag)
            return {}

        ai_text = ""
        if isinstance(envelope, dict):
            for container in (envelope, envelope.get("result", {}), envelope.get("data", {})):
                payloads = container.get("payloads")
                if isinstance(payloads, list) and len(payloads) > 0:
                    ai_text = payloads[0].get("text", "")
                    logger.info("[AGENT] ★ %s | 从 payloads[0].text 提取回复，长度=%s", skill_tag, len(ai_text))
                    break
            if not ai_text and isinstance(envelope.get("text"), str):
                ai_text = envelope["text"]
                logger.info("[AGENT] ★ %s | 从 envelope.text 提取回复，长度=%s", skill_tag, len(ai_text))
        elif isinstance(envelope, str):
            ai_text = envelope
            logger.info("[AGENT] ★ %s | 回复为纯字符串，长度=%s", skill_tag, len(ai_text))
        else:
            ai_text = stdout
            logger.info("[AGENT] ★ %s | 使用原始 stdout 作为回复", skill_tag)

        json_str = _extract_json_block(ai_text)
        if json_str:
            logger.info("[AGENT] ★ %s | 提取到 JSON，长度=%s", skill_tag, len(json_str))
            parsed = json.loads(json_str)
            logger.info("[AGENT] ★ %s | JSON 解析成功，keys=%s", skill_tag,
                         list(parsed.keys()) if isinstance(parsed, dict) else "not-dict")
            return parsed

        logger.warning("[AGENT] ★ %s | 未找到 JSON 块。回复[:500]: %s", skill_tag, ai_text[:500])
        return {}

    except subprocess.TimeoutExpired:
        logger.error("[AGENT] ★ %s | 调用超时 (timeout=%ss)", skill_tag, timeout)
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
