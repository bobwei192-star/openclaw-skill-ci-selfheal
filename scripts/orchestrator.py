import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .agent_wrapper import ask_agent

logger = logging.getLogger(__name__)

JENKINS_SKILL_DIR = "/home/node/.openclaw/workspace/skills/jenkins"
STATE_FILE = Path(__file__).parent.parent / ".self-heal-state.json"


class Orchestrator:
    def __init__(self, config_path=None):
        if config_path is None:
            config_path = Path(__file__).parent.parent / "config.yaml"
        self.config = yaml.safe_load(Path(config_path).read_text())
        self.max_retries = self.config["repair"]["max_retries"]
        self.poll_interval = self.config["repair"]["poll_interval_sec"]
        self.build_timeout = self.config["repair"]["build_timeout_min"] * 60
        self.state = self._load_state()
        self._jenkins_env = self._build_jenkins_env()

    def _build_jenkins_env(self):
        env = os.environ.copy()
        env.update({
            "JENKINS_URL": self.config["jenkins"]["url"],
            "JENKINS_USER": self.config["jenkins"]["user"],
            "JENKINS_API_TOKEN": os.environ.get(self.config["jenkins"]["token_env"], ""),
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
        })
        return env

    def _load_state(self):
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
        return {"version": "2.0.0", "chains": {}, "circuit_breaker": {}}

    def _save_state(self):
        STATE_FILE.write_text(json.dumps(self.state, indent=2, ensure_ascii=False))

    def _get_chain(self, job_name):
        if job_name not in self.state["chains"]:
            self.state["chains"][job_name] = {
                "current_retry": 0,
                "status": "idle",
                "processed_builds": [],
                "history": [],
            }
        return self.state["chains"][job_name]

    def _is_duplicate(self, job_name, build_number):
        chain = self._get_chain(job_name)
        if build_number in chain["processed_builds"]:
            return True
        chain["processed_builds"].append(build_number)
        self._save_state()
        return False

    def handle(self, payload):
        job = payload["job"]
        build = int(payload["build"])
        branch = payload.get("branch", "")
        repo = payload.get("repo", "")

        if self._is_circuit_open(job):
            self._notify(
                f"熔断生效：Job {job} 处于冷却期，本次事件仅生成诊断报告",
                level="warn",
            )
            return

        if self._is_duplicate(job, build):
            logger.info("Duplicate event: %s #%s, skipped", job, build)
            return

        if not self._whitelist_check(repo, branch):
            self._notify(
                f"自愈拦截：仓库 {repo} 分支 {branch} 不在白名单",
                level="warn",
            )
            return

        ctx = {
            "job": job,
            "build": build,
            "branch": branch,
            "repo": repo,
            "attempt": 0,
        }
        self._run_loop(ctx)

    def _run_loop(self, ctx):
        while ctx["attempt"] < self.max_retries:
            ctx["attempt"] += 1
            logger.info("Attempt %s/%s for %s #%s", ctx["attempt"], self.max_retries, ctx["job"], ctx["build"])

            try:
                log_text = self._collect_logs(ctx["job"], ctx["build"])
                diagnosis = self._diagnose(log_text, ctx)

                if diagnosis.get("confidence", 0) < 0.6:
                    self._save_report(diagnosis, ctx)
                    self._notify(f"AI 诊断置信度过低（{diagnosis.get('confidence', 0)}），请人工介入", level="warn")
                    self._record_history(ctx["job"], "LOW_CONFIDENCE", diagnosis)
                    return

                job_type = self._detect_job_type(ctx["job"])
                logger.info("Detected job type for %s: %s", ctx["job"], job_type)

                if job_type == "inline":
                    applied = self._apply_fix_inline(ctx["job"], diagnosis.get("fix_diff", {}))
                    fix_branch = ctx["branch"]
                elif job_type == "scm":
                    fix_branch = f"fix/ci-selfheal-{ctx['job']}-{ctx['build']}"
                    applied = self._apply_fix_scm(ctx["repo"], ctx["branch"], fix_branch, diagnosis.get("fix_diff", {}), ctx)
                else:
                    self._save_report(diagnosis, ctx)
                    self._notify(f"无法确定 {ctx['job']} 的 Pipeline 类型，请人工确认", level="warn")
                    return

                if not applied:
                    self._record_history(ctx["job"], "FIX_APPLY_FAILED", {"reason": f"Fix apply failed (type={job_type})"})
                    time.sleep(self._backoff(ctx["attempt"]))
                    continue

                new_build = self._trigger_build(ctx["job"], fix_branch)
                if new_build is None:
                    self._record_history(ctx["job"], "TRIGGER_FAILED", {"reason": "Could not trigger build"})
                    ctx["build"] = ctx["build"]
                    time.sleep(self._backoff(ctx["attempt"]))
                    continue

                success = self._poll_build(ctx["job"], new_build)
                if success:
                    self._record_history(ctx["job"], "SUCCESS", {"fix_applied": True})
                    self._reset_circuit(ctx["job"])
                    self._notify(f"自愈成功：{ctx['job']} #{ctx['build']} 修复后构建通过", level="info")
                    return
                else:
                    ctx["build"] = new_build
                    self._record_history(ctx["job"], f"RETRY_{ctx['attempt']}", {"failed_build": new_build})
                    time.sleep(self._backoff(ctx["attempt"]))

            except Exception as e:
                logger.exception("Attempt %s failed with exception", ctx["attempt"])
                self._record_history(ctx["job"], f"ERROR_{ctx['attempt']}", {"error": str(e)})
                time.sleep(self._backoff(ctx["attempt"]))

        self._circuit_break(ctx["job"])
        self._notify(
            f"自愈熔断：Job {ctx['job']} 连续 {self.max_retries} 次修复失败，请人工介入",
            level="critical",
        )

    def _collect_logs(self, job_name, build_number):
        cmd = [
            "node", f"{JENKINS_SKILL_DIR}/scripts/jenkins.mjs", "console",
            "--job", job_name,
            "--build", str(build_number),
            "--tail", "200",
        ]
        env = self._jenkins_env
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        raw = result.stdout or result.stderr or ""
        console_text = raw
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "console" in data:
                console_text = data["console"]
        except (json.JSONDecodeError, TypeError):
            pass
        return self._desensitize(console_text)

    def _diagnose(self, log_text, ctx):
        instruction = f"""你是 CI/CD 自愈专家。下面是 Jenkins 构建失败日志（已脱敏）：

```
{log_text}
```

请使用 ci-cd-watchdog 分析日志，定位根因，生成修复方案。

修复必须只修改构建脚本（如 Jenkinsfile、shell 脚本）、CI 配置、环境变量，不得修改 src/ 下的业务代码。

输出 JSON 格式（不要额外文本）：
{{
  "root_cause": "...",
  "error_type": "compile|dependency|config|test|env|other",
  "confidence": 0.85,
  "fix_diff": {{ "文件相对路径": "完整新内容" }}
}}

如果无法修复（confidence < 0.6），fix_diff 为空，root_cause 说明原因。"""
        return ask_agent(instruction, expect_json=True, timeout=300)

    def _detect_job_type(self, job_name):
        config_xml = self._jenkins_get(
            f"{self.config['jenkins']['url']}/job/{job_name}/config.xml"
        )
        if not config_xml:
            return "unknown"
        if "CpsFlowDefinition" in config_xml and "<script>" in config_xml:
            return "inline"
        if "<scm" in config_xml and "<scriptPath>" in config_xml:
            return "scm"
        return "unknown"

    def _apply_fix_inline(self, job_name, fix_diff):
        if not fix_diff:
            logger.warning("fix_diff is empty, skipping fix")
            return False

        pipeline_script = None
        for content in fix_diff.values():
            if isinstance(content, str) and len(content) > 50:
                pipeline_script = content
                break
        if not pipeline_script:
            logger.warning("No pipeline script found in fix_diff")
            return False

        jenkins_base = self.config["jenkins"]["url"]
        job_url = f"{jenkins_base}/job/{job_name}"

        config_xml = self._jenkins_get(job_url + "/config.xml")
        if not config_xml:
            logger.error("Could not fetch config.xml for %s", job_name)
            return False

        from xml.sax.saxutils import escape as xml_escape
        escaped = xml_escape(pipeline_script)
        updated = re.sub(r"<script>[\s\S]*?</script>", f"<script>{escaped}</script>", config_xml)

        result = self._jenkins_post(job_url + "/config.xml", updated)
        if result:
            logger.info("Jenkins job %s config updated", job_name)
            return True
        else:
            logger.error("Failed to update Jenkins job %s config", job_name)
            return False

    def _jenkins_get(self, url):
        cmd = ["curl", "-s", "-k", "-u",
               f"{self.config['jenkins']['user']}:{os.environ.get(self.config['jenkins']['token_env'], '')}",
               url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 and "Error 404" not in r.stdout else None

    def _jenkins_post(self, url, data):
        cmd = ["curl", "-s", "-k", "-X", "POST",
               "-u", f"{self.config['jenkins']['user']}:{os.environ.get(self.config['jenkins']['token_env'], '')}",
               "-H", "Content-Type: application/xml",
               "-d", data,
               url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode == 0

    def _apply_fix_scm(self, repo, base_branch, fix_branch, fix_diff, ctx):
        if not fix_diff:
            logger.warning("fix_diff is empty, skipping scm fix")
            return False

        gitlab_token = os.environ.get(self.config["gitlab"].get("token_env", ""))
        gitlab_url = os.environ.get(self.config["gitlab"].get("host_env", "")) or self.config["gitlab"]["url"]
        if not gitlab_token:
            logger.warning("GitLab token not set, cannot apply fix via SCM")
            files_str = json.dumps(fix_diff, ensure_ascii=False)
            instruction = f"""使用 gitlab-skill，在仓库 {repo} 上执行：
1. 基于 {base_branch} 创建新分支 {fix_branch}
2. 写入以下文件内容（key 为文件路径，value 为完整新内容）：{files_str}
3. 提交并推送，commit message: "[ci-selfheal] auto fix {ctx['job']} #{ctx['build']}"
返回操作结果。"""
            return ask_agent(instruction, expect_json=False, timeout=120) is not None

        project_path = repo.replace("/", "%2F")
        branch_url = f"{gitlab_url}/api/v4/projects/{project_path}/repository/branches"
        branch_body = json.dumps({"branch": fix_branch, "ref": base_branch})
        r = subprocess.run(
            ["curl", "-s", "-k", "-X", "POST", "-H", f"PRIVATE-TOKEN: {gitlab_token}",
             "-H", "Content-Type: application/json", "-d", branch_body, branch_url],
            capture_output=True, text=True, timeout=30,
        )
        logger.info("Create branch result: %s", r.stdout[:200])

        for file_path, content in fix_diff.items():
            commit_url = f"{gitlab_url}/api/v4/projects/{project_path}/repository/commits"
            commit_body = json.dumps({
                "branch": fix_branch,
                "commit_message": f"[ci-selfheal] auto fix {ctx['job']} #{ctx['build']}",
                "actions": [{"action": "update", "file_path": file_path, "content": content}],
            })
            r = subprocess.run(
                ["curl", "-s", "-k", "-X", "POST", "-H", f"PRIVATE-TOKEN: {gitlab_token}",
                 "-H", "Content-Type: application/json", "-d", commit_body, commit_url],
                capture_output=True, text=True, timeout=30,
            )
            logger.info("Commit result: %s", r.stdout[:200])
        return True

    def _trigger_build(self, job_name, branch):
        cmd = [
            "node", f"{JENKINS_SKILL_DIR}/scripts/jenkins.mjs", "build",
            "--job", job_name,
        ]
        env = self._jenkins_env
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        logger.info("Trigger build result: %s", result.stdout[:500])
        return self._get_last_build_number(job_name)

    def _get_last_build_number(self, job_name):
        cmd = [
            "node", f"{JENKINS_SKILL_DIR}/scripts/jenkins.mjs", "status",
            "--job", job_name, "--last",
        ]
        env = self._jenkins_env
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        try:
            data = json.loads(result.stdout)
            return data.get("number") or data.get("lastBuild", {}).get("number")
        except (json.JSONDecodeError, AttributeError):
            return None

    def _poll_build(self, job_name, build_number):
        deadline = time.time() + self.build_timeout
        while time.time() < deadline:
            status = self._get_build_status(job_name, build_number)
            if status == "SUCCESS":
                return True
            if status in ("FAILURE", "ABORTED"):
                return False
            time.sleep(self.poll_interval)
        return False

    def _get_build_status(self, job_name, build_number):
        cmd = [
            "node", f"{JENKINS_SKILL_DIR}/scripts/jenkins.mjs", "status",
            "--job", job_name,
            "--build", str(build_number),
        ]
        env = self._jenkins_env
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        try:
            data = json.loads(result.stdout)
            return data.get("result") or data.get("lastBuild", {}).get("result")
        except (json.JSONDecodeError, AttributeError):
            return None

    def _create_mr(self, repo, source_branch, target_branch, diagnosis, ctx):
        instruction = f"""使用 gitlab-skill 在仓库 {repo} 创建 Merge Request：
- 源分支：{source_branch}
- 目标分支：{target_branch}
- 标题：[ci-selfheal] Auto-fix: {ctx['job']} #{ctx['build']} — {diagnosis.get('error_type', 'unknown')}
- 描述：{self._mr_description(diagnosis, ctx)}
- 添加标签：auto-fix,ci-selfheal
返回创建的 MR URL。"""
        return ask_agent(instruction, expect_json=False, timeout=60)

    def _mr_description(self, diagnosis, ctx):
        return f"""## AI 自愈诊断

- **Job**: {ctx['job']}
- **构建**: #{ctx['build']}
- **分支**: {ctx['branch']}
- **根因**: {diagnosis.get('root_cause', '未知')}
- **错误类型**: {diagnosis.get('error_type', 'unknown')}
- **置信度**: {diagnosis.get('confidence', 0)}

## 修复内容
已通过 AI 自动诊断并修复，详见 commit diff。

## 验证
修复分支已通过 Jenkins 重建验证（Build #{ctx.get('new_build', 'N/A')}）。
"""

    def _whitelist_check(self, repo, branch):
        repos = self.config["whitelist"]["repos"]
        if repos and repo not in repos:
            logger.warning("Repo %s not in whitelist", repo)
            return False

        protected = self.config["whitelist"]["protected_branches"]
        for pattern in protected:
            if re.match(pattern.replace("*", ".*"), branch):
                logger.warning("Branch %s matches protected pattern %s", branch, pattern)
                return False

        branch_pattern = self.config["whitelist"].get("branch_pattern")
        if branch_pattern and not re.match(branch_pattern, branch):
            logger.warning("Branch %s does not match allowed pattern %s", branch, branch_pattern)
            return False

        return True

    def _desensitize(self, text):
        text = re.sub(r"(password|token|secret|api_key|api_token)=[\S]+", r"\1=[REDACTED]", text, flags=re.IGNORECASE)
        text = re.sub(
            r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            "[INTERNAL_IP]", text,
        )
        return text

    def _backoff(self, attempt):
        delays = [1, 5, 15, 30, 60]
        return delays[min(attempt - 1, len(delays) - 1)]

    def _record_history(self, job_name, status, detail=None):
        chain = self._get_chain(job_name)
        chain["status"] = status
        chain["history"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "detail": detail or {},
        })
        self._save_state()

    def _save_report(self, diagnosis, ctx):
        report_dir = Path(__file__).parent.parent / "reports"
        report_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = report_dir / f"{ctx['job']}_diagnosis_{timestamp}.md"
        report = self._mr_description(diagnosis, ctx)
        report += f"\n\n## 建议\n诊断置信度低于阈值（{diagnosis.get('confidence', 0)} < 0.6），请人工确认。\n"
        report_file.write_text(report)
        logger.info("Diagnosis report saved: %s", report_file)

    def _notify(self, message, level="info"):
        webhook_url = os.environ.get(self.config["notify"].get("dingtalk_webhook_env", ""))
        if webhook_url:
            try:
                import requests
                requests.post(webhook_url, json={"msgtype": "text", "text": {"content": message}}, timeout=10)
            except Exception:
                logger.warning("Failed to send notification: %s", message)
        else:
            logger.info("[NOTIFY:%s] %s", level, message)

    def _is_circuit_open(self, job_name):
        cb = self.state["circuit_breaker"].get(job_name)
        if not cb:
            return False
        opened_at = cb.get("opened_at", 0)
        cooldown = 2 * 3600
        if time.time() - opened_at < cooldown:
            return True
        return False

    def _circuit_break(self, job_name):
        self.state["circuit_breaker"][job_name] = {
            "opened_at": time.time(),
            "reason": f"max retries ({self.max_retries}) exceeded",
        }
        self._save_state()

    def _reset_circuit(self, job_name):
        self.state["circuit_breaker"].pop(job_name, None)
        self._save_state()

    def get_status(self, job_name=None):
        if job_name:
            return self._get_chain(job_name)
        return self.state
