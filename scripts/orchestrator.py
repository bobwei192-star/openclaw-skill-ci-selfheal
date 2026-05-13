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
            "JENKINS_URL": os.environ.get(self.config["jenkins"]["url_env"], ""),
            "JENKINS_USER": os.environ.get(self.config["jenkins"]["user_env"], ""),
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

        logger.info("=" * 60)
        logger.info("[HANDLE] 收到自愈事件: job=%s build=%s branch=%s repo=%s", job, build, branch, repo)
        logger.info("[CONFIG] max_retries=%s poll_interval=%ss build_timeout=%ss",
                     self.max_retries, self.poll_interval, self.build_timeout)

        if self._is_circuit_open(job):
            logger.info("[HANDLE] 熔断生效: Job %s 处于冷却期，跳过自愈", job)
            self._notify(
                f"熔断生效：Job {job} 处于冷却期，本次事件仅生成诊断报告",
                level="warn",
            )
            return

        if self._is_duplicate(job, build):
            logger.info("[HANDLE] 重复事件: %s #%s，跳过", job, build)
            return

        logger.info("[HANDLE] 白名单检查: repo=%s branch=%s", repo, branch)
        if not self._whitelist_check(repo, branch):
            logger.warning("[HANDLE] 白名单拦截: repo=%s branch=%s", repo, branch)
            self._notify(
                f"自愈拦截：仓库 {repo} 分支 {branch} 不在白名单",
                level="warn",
            )
            return
        logger.info("[HANDLE] 白名单检查: 通过")

        ctx = {
            "job": job,
            "build": build,
            "branch": branch,
            "repo": repo,
            "attempt": 0,
        }
        logger.info("[HANDLE] 开始自愈循环: ctx=%s", json.dumps(ctx, ensure_ascii=False))
        self._run_loop(ctx)

    def _run_loop(self, ctx):
        while ctx["attempt"] < self.max_retries:
            ctx["attempt"] += 1
            logger.info("-" * 40)
            logger.info("[LOOP] 第 %s/%s 次尝试: %s #%s", ctx["attempt"], self.max_retries, ctx["job"], ctx["build"])

            try:
                logger.info("[STEP 1/6] 收集 Jenkins 构建日志: %s #%s", ctx["job"], ctx["build"])
                log_text = self._collect_logs(ctx["job"], ctx["build"])
                logger.info("[STEP 1/6] 日志收集完成，长度=%s 字符", len(log_text))

                logger.info("[STEP 2/6] AI 诊断: 调用 ask_agent() → openclaw agent --agent main")
                diagnosis = self._diagnose(log_text, ctx)
                logger.info("[STEP 2/6] AI 诊断完成: root_cause=%s confidence=%s error_type=%s",
                             diagnosis.get("root_cause", "N/A"),
                             diagnosis.get("confidence", 0),
                             diagnosis.get("error_type", "N/A"))

                if diagnosis.get("confidence", 0) < 0.6:
                    logger.warning("[LOOP] 置信度过低 (%.2f < 0.6)，保存诊断报告并退出", diagnosis.get("confidence", 0))
                    self._save_report(diagnosis, ctx)
                    self._notify(f"AI 诊断置信度过低（{diagnosis.get('confidence', 0)}），请人工介入", level="warn")
                    self._record_history(ctx["job"], "LOW_CONFIDENCE", diagnosis)
                    return

                logger.info("[STEP 3/6] 检测 Job 类型: %s", ctx["job"])
                job_type = self._detect_job_type(ctx["job"])
                logger.info("[STEP 3/6] Job 类型: %s", job_type)

                if job_type == "inline":
                    logger.info("[STEP 4/6] 应用修复 (inline): 直接更新 Jenkins Job 配置")
                    applied = self._apply_fix_inline(ctx["job"], diagnosis.get("fix_diff", {}))
                    logger.info("[STEP 4/6] inline 修复结果: %s", "成功" if applied else "失败")

                    if not applied and ctx.get("repo"):
                        logger.info("[STEP 4/6] inline 修复失败，回退到 SCM 路径 (直接提交到 %s 分支)", ctx["branch"])
                        fix_branch = ctx["branch"]
                        applied = self._apply_fix_scm(ctx["repo"], ctx["branch"], fix_branch, diagnosis.get("fix_diff", {}), ctx)
                        logger.info("[STEP 4/6] SCM 回退修复结果: %s", "成功" if applied else "失败")
                    else:
                        fix_branch = ctx["branch"]

                elif job_type == "scm":
                    fix_branch = f"fix/ci-selfheal-{ctx['job']}-{ctx['build']}"
                    logger.info("[STEP 4/6] 应用修复 (SCM): repo=%s base=%s fix_branch=%s", ctx["repo"], ctx["branch"], fix_branch)
                    applied = self._apply_fix_scm(ctx["repo"], ctx["branch"], fix_branch, diagnosis.get("fix_diff", {}), ctx)
                    logger.info("[STEP 4/6] 修复应用结果: %s", "成功" if applied else "失败")
                else:
                    logger.error("[LOOP] 无法确定 Job 类型: %s", job_type)
                    self._save_report(diagnosis, ctx)
                    self._notify(f"无法确定 {ctx['job']} 的 Pipeline 类型，请人工确认", level="warn")
                    return

                if not applied:
                    logger.warning("[LOOP] 修复应用失败，backoff=%ss 后重试", self._backoff(ctx["attempt"]))
                    self._record_history(ctx["job"], "FIX_APPLY_FAILED", {"reason": f"Fix apply failed (type={job_type})"})
                    time.sleep(self._backoff(ctx["attempt"]))
                    continue

                logger.info("[STEP 5/6] 触发验证构建: job=%s branch=%s", ctx["job"], fix_branch)
                new_build = self._trigger_build(ctx["job"], fix_branch)
                logger.info("[STEP 5/6] 验证构建: new_build=%s", new_build)

                if new_build is None:
                    logger.error("[LOOP] 触发构建失败，backoff=%ss 后重试", self._backoff(ctx["attempt"]))
                    self._record_history(ctx["job"], "TRIGGER_FAILED", {"reason": "Could not trigger build"})
                    ctx["build"] = ctx["build"]
                    time.sleep(self._backoff(ctx["attempt"]))
                    continue

                logger.info("[STEP 6/6] 轮询验证构建: %s #%s (超时=%ss)", ctx["job"], new_build, self.build_timeout)
                success = self._poll_build(ctx["job"], new_build)
                logger.info("[STEP 6/6] 验证构建结果: %s", "SUCCESS" if success else "FAILURE")

                if success:
                    if fix_branch != ctx["branch"]:
                        logger.info("[EXTRA] 创建 Merge Request: %s → %s", fix_branch, ctx["branch"])
                        self._create_mr(ctx["repo"], fix_branch, ctx["branch"], diagnosis, ctx)
                    self._record_history(ctx["job"], "SUCCESS", {"fix_applied": True, "job_type": job_type})
                    self._reset_circuit(ctx["job"])
                    self._notify(f"自愈成功：{ctx['job']} #{ctx['build']} 修复后构建通过", level="info")
                    logger.info("=" * 60)
                    logger.info("[RESULT] ★★★ 自愈成功！%s #%s → 修复分支 %s", ctx["job"], ctx["build"], fix_branch)
                    logger.info("=" * 60)
                    return
                else:
                    logger.warning("[LOOP] 验证构建仍失败，backoff=%ss 后进入下一轮重试", self._backoff(ctx["attempt"]))
                    ctx["build"] = new_build
                    self._record_history(ctx["job"], f"RETRY_{ctx['attempt']}", {"failed_build": new_build})
                    time.sleep(self._backoff(ctx["attempt"]))

            except Exception as e:
                logger.exception("[LOOP] 第 %s 次尝试异常: %s", ctx["attempt"], str(e))
                self._record_history(ctx["job"], f"ERROR_{ctx['attempt']}", {"error": str(e)})
                time.sleep(self._backoff(ctx["attempt"]))

        logger.error("=" * 60)
        logger.error("[RESULT] ★★★ 自愈熔断！%s 连续 %s 次修复失败", ctx["job"], self.max_retries)
        logger.error("=" * 60)
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
        logger.info("[COLLECT] ★ skill=jenkins-skill | 脚本: %s", " ".join(cmd))
        logger.info("[COLLECT] ★ skill=jenkins-skill | 环境: JENKINS_URL=%s JENKINS_USER=%s",
                     self._jenkins_env.get("JENKINS_URL", "N/A"),
                     self._jenkins_env.get("JENKINS_USER", "N/A"))
        env = self._jenkins_env
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        raw = result.stdout or result.stderr or ""
        logger.info("[COLLECT] ★ skill=jenkins-skill | exit=%s stdout=%s stderr=%s",
                     result.returncode, len(result.stdout or ""), len(result.stderr or ""))
        console_text = raw
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "console" in data:
                console_text = data["console"]
                logger.info("[COLLECT] ★ skill=jenkins-skill | console 字段长度=%s", len(console_text))
        except (json.JSONDecodeError, TypeError):
            logger.warning("[COLLECT] ★ skill=jenkins-skill | JSON 解析失败，使用原始输出")
            pass
        desensitized = self._desensitize(console_text)
        logger.info("[COLLECT] ★ skill=jenkins-skill | 脱敏后 %s 字符", len(desensitized))
        return desensitized

    def _diagnose(self, log_text, ctx):
        instruction = f"""你是 CI/CD 自愈专家。下面是 Jenkins 构建失败日志（已脱敏）：

```
{log_text}
```

请使用 ci-cd-watchdog 分析日志，定位根因，生成修复方案。

规则：
1. 只修改构建脚本（Jenkinsfile、shell 脚本、Makefile 等）、CI 配置、环境变量，不得修改 src/ 下的业务代码
2. fix_diff 的 key 必须是仓库中的实际文件相对路径（如 test_function.sh、Jenkinsfile），value 是文件的完整修复后内容
3. 如果错误来自 shell 脚本语法问题（如缺少 fi），修复该脚本文件，不要修改 Jenkinsfile/流水线本身

输出 JSON 格式（不要额外文本）：
{{
  "root_cause": "...",
  "error_type": "compile|dependency|config|test|env|other",
  "confidence": 0.85,
  "fix_diff": {{ "文件相对路径": "完整新内容" }}
}}

如果无法修复（confidence < 0.6），fix_diff 为空，root_cause 说明原因。"""
        logger.info("[DIAGNOSE] ★ skill=ci-cd-watchdog | 指令长度=%s", len(instruction))
        logger.info("[DIAGNOSE] ★ skill=ci-cd-watchdog | 指令预览: %s...", instruction[:300])
        result = ask_agent(instruction, expect_json=True, timeout=300, skill_name="ci-cd-watchdog")
        logger.info("[DIAGNOSE] ★ skill=ci-cd-watchdog | 返回 keys=%s", list(result.keys()) if isinstance(result, dict) else "non-dict")
        return result

    def _jenkins_url(self):
        return os.environ.get(self.config["jenkins"]["url_env"], "")

    def _jenkins_user(self):
        return os.environ.get(self.config["jenkins"]["user_env"], "")

    def _detect_job_type(self, job_name):
        config_url = f"{self._jenkins_url()}/job/{job_name}/config.xml"
        logger.info("[DETECT] ★ skill=jenkins-api | 获取配置: %s", config_url)
        config_xml = self._jenkins_get(config_url)
        if not config_xml:
            logger.warning("[DETECT] ★ skill=jenkins-api | 无法获取 config.xml")
            return "unknown"
        logger.info("[DETECT] ★ skill=jenkins-api | config.xml 长度=%s", len(config_xml))
        if "CpsFlowDefinition" in config_xml and "<script>" in config_xml:
            logger.info("[DETECT] ★ skill=jenkins-api | 判定为 inline (Pipeline script)")
            return "inline"
        if "<scm" in config_xml and "<scriptPath>" in config_xml:
            logger.info("[DETECT] ★ skill=jenkins-api | 判定为 scm (Pipeline script from SCM)")
            return "scm"
        logger.warning("[DETECT] ★ skill=jenkins-api | 无法判定类型，返回 unknown")
        return "unknown"

    def _apply_fix_inline(self, job_name, fix_diff):
        if not fix_diff:
            logger.warning("[FIX_INLINE] fix_diff 为空，跳过")
            return False

        pipeline_script = None
        pipeline_file = None
        for file_path, content in fix_diff.items():
            logger.info("[FIX_INLINE] ★ skill=jenkins-api | fix_diff 文件: %s (长度=%s)", file_path, len(str(content)))
            if not isinstance(content, str) or len(str(content)) < 50:
                continue
            content_str = str(content)
            if re.search(r'\b(pipeline\s*\{|pipeline\s*\(|node\s*\{|node\s*\()', content_str):
                pipeline_script = content_str
                pipeline_file = file_path
                break
        if not pipeline_script:
            logger.warning("[FIX_INLINE] ★ skill=jenkins-api | fix_diff 不包含 Pipeline 脚本 (Jenkinsfile/Groovy)")
            return False

        logger.info("[FIX_INLINE] ★ skill=jenkins-api | 检测到 Pipeline 脚本: %s (长度=%s)", pipeline_file, len(pipeline_script))
        jenkins_base = self._jenkins_url()
        job_url = f"{jenkins_base}/job/{job_name}"
        logger.info("[FIX_INLINE] ★ skill=jenkins-api | 获取 config.xml: %s/config.xml", job_url)

        config_xml = self._jenkins_get(job_url + "/config.xml")
        if not config_xml:
            logger.error("[FIX_INLINE] ★ skill=jenkins-api | 无法获取 %s 的 config.xml", job_name)
            return False

        from xml.sax.saxutils import escape as xml_escape
        escaped = xml_escape(pipeline_script)
        updated = re.sub(r"<script>[\s\S]*?</script>", f"<script>{escaped}</script>", config_xml)
        logger.info("[FIX_INLINE] ★ skill=jenkins-api | 更新 config.xml (新脚本长度=%s)", len(pipeline_script))

        result = self._jenkins_post(job_url + "/config.xml", updated)
        if result:
            logger.info("[FIX_INLINE] ★ skill=jenkins-api | %s 配置更新成功", job_name)
            return True
        else:
            logger.error("[FIX_INLINE] ★ skill=jenkins-api | %s 配置更新失败", job_name)
            return False

    def _jenkins_get(self, url):
        cmd = ["curl", "-s", "-k", "-u",
               f"{self._jenkins_user()}:{os.environ.get(self.config['jenkins']['token_env'], '')}",
               url]
        logger.info("[JENKINS_GET] ★ skill=jenkins-api | curl -s -k -u %s:*** %s", self._jenkins_user(), url)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0 and "Error 404" not in r.stdout
        logger.info("[JENKINS_GET] ★ skill=jenkins-api | exit=%s ok=%s len=%s", r.returncode, ok, len(r.stdout or ""))
        return r.stdout if ok else None

    def _jenkins_post(self, url, data):
        cmd = ["curl", "-s", "-k", "-X", "POST",
               "-u", f"{self._jenkins_user()}:{os.environ.get(self.config['jenkins']['token_env'], '')}",
               "-H", "Content-Type: application/xml",
               "-d", data,
               url]
        logger.info("[JENKINS_POST] ★ skill=jenkins-api | curl -s -k -X POST -u %s:*** %s (data len=%s)", self._jenkins_user(), url, len(data))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        logger.info("[JENKINS_POST] ★ skill=jenkins-api | exit=%s len=%s", r.returncode, len(r.stdout or ""))
        return r.returncode == 0

    def _apply_fix_scm(self, repo, base_branch, fix_branch, fix_diff, ctx):
        if not fix_diff:
            logger.warning("[FIX_SCM] fix_diff 为空，跳过")
            return False

        gitlab_token = os.environ.get(self.config["gitlab"].get("token_env", ""))
        gitlab_url = os.environ.get(self.config["gitlab"].get("host_env", "")) or self.config["gitlab"]["url"]
        logger.info("[FIX_SCM] GitLab URL=%s Token=%s", gitlab_url, "***" if gitlab_token else "NOT SET")

        if not gitlab_token:
            logger.warning("[FIX_SCM] ★ skill=gitlab-skill | ask_agent 回退模式")
            files_str = json.dumps(fix_diff, ensure_ascii=False)
            instruction = f"""使用 gitlab-skill，在仓库 {repo} 上执行：
1. 基于 {base_branch} 创建新分支 {fix_branch}
2. 写入以下文件内容（key 为文件路径，value 为完整新内容）：{files_str}
3. 提交并推送，commit message: "[ci-selfheal] auto fix {ctx['job']} #{ctx['build']}"
返回操作结果。"""
            logger.info("[FIX_SCM] ★ skill=gitlab-skill | 指令长度=%s", len(instruction))
            result = ask_agent(instruction, expect_json=False, timeout=120, skill_name="gitlab-skill")
            logger.info("[FIX_SCM] ★ skill=gitlab-skill | 返回长度=%s", len(result) if result else 0)
            return result is not None

        project_path = repo.replace("/", "%2F")

        if fix_branch == base_branch:
            logger.info("[FIX_SCM] ★ skill=gitlab-api | fix_branch==base_branch (%s)，跳过创建分支，直接提交到现有分支", fix_branch)
        else:
            branch_url = f"{gitlab_url}/api/v4/projects/{project_path}/repository/branches"
            branch_body = json.dumps({"branch": fix_branch, "ref": base_branch})
            logger.info("[FIX_SCM] ★ skill=gitlab-api | 创建分支: POST %s body=%s", branch_url, branch_body)
            r = subprocess.run(
                ["curl", "-s", "-k", "-X", "POST", "-H", f"PRIVATE-TOKEN: {gitlab_token}",
                 "-H", "Content-Type: application/json", "-d", branch_body, branch_url],
                capture_output=True, text=True, timeout=30,
            )
            logger.info("[FIX_SCM] ★ skill=gitlab-api | 创建分支结果 (exit=%s): %s", r.returncode, r.stdout[:300])

        for file_path, content in fix_diff.items():
            commit_url = f"{gitlab_url}/api/v4/projects/{project_path}/repository/commits"
            commit_body = json.dumps({
                "branch": fix_branch,
                "commit_message": f"[ci-selfheal] auto fix {ctx['job']} #{ctx['build']}",
                "actions": [{"action": "update", "file_path": file_path, "content": content}],
            })
            logger.info("[FIX_SCM] ★ skill=gitlab-api | 提交文件: POST %s file=%s content_len=%s", commit_url, file_path, len(content))
            r = subprocess.run(
                ["curl", "-s", "-k", "-X", "POST", "-H", f"PRIVATE-TOKEN: {gitlab_token}",
                 "-H", "Content-Type: application/json", "-d", commit_body, commit_url],
                capture_output=True, text=True, timeout=30,
            )
            logger.info("[FIX_SCM] ★ skill=gitlab-api | 提交结果 (exit=%s): %s", r.returncode, r.stdout[:300])
        return True

    def _trigger_build(self, job_name, branch):
        cmd = [
            "node", f"{JENKINS_SKILL_DIR}/scripts/jenkins.mjs", "build",
            "--job", job_name,
            "--param", "BUILD_TYPE=selfheal",
        ]
        logger.info("[TRIGGER] ★ skill=jenkins-skill | 脚本: %s JENKINS_URL=%s", " ".join(cmd),
                     self._jenkins_env.get("JENKINS_URL", "N/A"))
        env = self._jenkins_env
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        logger.info("[TRIGGER] ★ skill=jenkins-skill | exit=%s stdout=%s", result.returncode, result.stdout[:500])
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
        poll_count = 0
        while time.time() < deadline:
            poll_count += 1
            status = self._get_build_status(job_name, build_number)
            if poll_count == 1 or status in ("SUCCESS", "FAILURE", "ABORTED"):
                logger.info("[POLL] ★ skill=jenkins-skill | #%s %s #%s status=%s elapsed=%ss",
                             poll_count, job_name, build_number, status,
                             int(self.build_timeout - (deadline - time.time())))
            if status == "SUCCESS":
                return True
            if status in ("FAILURE", "ABORTED"):
                return False
            time.sleep(self.poll_interval)
        logger.warning("[POLL] ★ skill=jenkins-skill | 轮询超时: %s #%s (timeout=%ss)", job_name, build_number, self.build_timeout)
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
        title = f"[ci-selfheal] Auto-fix: {ctx['job']} #{ctx['build']} — {diagnosis.get('error_type', 'unknown')}"
        description = self._mr_description(diagnosis, ctx)

        gitlab_token = os.environ.get(self.config["gitlab"].get("token_env", ""))
        gitlab_url = os.environ.get(self.config["gitlab"].get("host_env", "")) or self.config["gitlab"]["url"]

        logger.info("[MR] 创建 MR: %s → %s title=%s", source_branch, target_branch, title)
        logger.info("[MR] GitLab URL=%s Token=%s", gitlab_url, "***" if gitlab_token else "NOT SET")

        if gitlab_token:
            project_path = repo.replace("/", "%2F")
            mr_url = f"{gitlab_url}/api/v4/projects/{project_path}/merge_requests"
            logger.info("[MR] ★ skill=gitlab-api | POST %s source=%s target=%s", mr_url, source_branch, target_branch)
            result = subprocess.run(
                ["curl", "-s", "-k", "-X", "POST",
                 "-H", f"PRIVATE-TOKEN: {gitlab_token}",
                 "--data-urlencode", f"source_branch={source_branch}",
                 "--data-urlencode", f"target_branch={target_branch}",
                 "--data-urlencode", f"title={title}",
                 "--data-urlencode", f"description={description}",
                 "--data-urlencode", "labels=auto-fix,ci-selfheal",
                 mr_url],
                capture_output=True, text=True, timeout=30,
            )
            logger.info("[MR] ★ skill=gitlab-api | 结果 (exit=%s): %s", result.returncode, result.stdout[:300])
            if "web_url" in result.stdout:
                logger.info("[MR] MR 创建成功")
                return True

        logger.info("[MR] ★ skill=gitlab-skill | ask_agent 回退模式")
        instruction = f"""使用 gitlab-skill 在仓库 {repo} 创建 Merge Request：
- 源分支：{source_branch}
- 目标分支：{target_branch}
- 标题：{title}
- 描述：{description}
- 添加标签：auto-fix,ci-selfheal
返回创建的 MR URL。"""
        result = ask_agent(instruction, expect_json=False, timeout=60, skill_name="gitlab-skill")
        logger.info("[MR] ★ skill=gitlab-skill | 返回长度=%s", len(result) if result else 0)
        return result is not None

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
        logger.info("[WHITELIST] repos=%s branch=%s branch_pattern=%s protected=%s",
                     repos, branch,
                     self.config["whitelist"].get("branch_pattern", ""),
                     self.config["whitelist"]["protected_branches"])
        if repos and repo not in repos:
            logger.warning("[WHITELIST] Repo %s 不在白名单 %s", repo, repos)
            return False

        protected = self.config["whitelist"]["protected_branches"]
        for pattern in protected:
            if "*" in pattern:
                if re.match(pattern.replace("*", ".*") + "$", branch):
                    logger.warning("[WHITELIST] Branch %s 匹配保护模式 %s", branch, pattern)
                    return False
            else:
                if pattern == branch:
                    logger.warning("[WHITELIST] Branch %s 是保护分支", branch)
                    return False

        branch_pattern = self.config["whitelist"].get("branch_pattern")
        if branch_pattern and not re.match(branch_pattern, branch):
            logger.warning("[WHITELIST] Branch %s 不匹配允许模式 %s", branch, branch_pattern)
            return False

        logger.info("[WHITELIST] 检查通过")
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
        chain = self._get_chain(job_name)
        chain["processed_builds"] = []
        self.state["circuit_breaker"].pop(job_name, None)
        self._save_state()

    def get_status(self, job_name=None):
        if job_name:
            chain = self._get_chain(job_name)
            return {
                **chain,
                "circuit_open": self._is_circuit_open(job_name),
            }
        summary = {"version": self.state.get("version", "2.0.0"), "jobs": {}}
        for name, chain in self.state.get("chains", {}).items():
            summary["jobs"][name] = {
                "status": chain.get("status", "unknown"),
                "history_count": len(chain.get("history", [])),
                "circuit_open": self._is_circuit_open(name),
            }
        return summary
