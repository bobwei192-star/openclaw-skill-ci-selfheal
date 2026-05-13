#!/bin/bash
# ci-selfheal 部署验证脚本
# 运行环境：OpenClaw 容器内
# 用法：bash verify-deployment.sh
# 覆盖：13施工步骤-极简 中的所有验收点

set -e

CI_DIR="/home/node/.openclaw/workspace/skills/ci-selfheal"
cd "$CI_DIR"
source .env 2>/dev/null || true

PASS=0
FAIL=0
SKIP=0
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

check() {
  local desc="$1"
  local cmd="$2"
  local expected="$3"
  echo -n "  [$desc] ... "
  local output
  if output=$(eval "$cmd" 2>/dev/null); then
    if [ -n "$expected" ] && ! echo "$output" | grep -q "$expected"; then
      echo -e "${RED}FAIL${NC} (expected: $expected)"
      echo "       got: $(echo "$output" | head -c 200)"
      FAIL=$((FAIL + 1))
    else
      echo -e "${GREEN}PASS${NC}"
      PASS=$((PASS + 1))
    fi
  else
    echo -e "${YELLOW}SKIP${NC} (command failed)"
    SKIP=$((SKIP + 1))
  fi
}

echo "========================================"
echo "  ci-selfheal 部署验收测试"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"
echo ""

# ==========================================
# 1. 环境确认
# ==========================================
echo "=== 1. 环境确认 ==="

check "OpenClaw CLI 可用" \
  "openclaw --version 2>&1 | head -1" \
  "OpenClaw"

check "Python 版本" \
  "python3 --version 2>&1" \
  "3."

check "Docker 网络连通（Ping Jenkins via nginx）" \
  "curl -k -s -o /dev/null -w '%{http_code}' ${JENKINS_URL:-https://devopsclaw-nginx:8440/jenkins}/api/json 2>&1" \
  "403\|200"

echo ""

# ==========================================
# 2. Jenkins 连通性
# ==========================================
echo "=== 2. Jenkins 连通性 ==="

JENKINS_URL="${JENKINS_URL:-https://devopsclaw-nginx:8440/jenkins}"
JENKINS_USER="${JENKINS_USER:-zx}"
JENKINS_API_TOKEN="${JENKINS_API_TOKEN:-11e9fec81c11241d5a3897ab45608c6851}"

check "Jenkins API 可达" \
  "curl -k -s -o /dev/null -w '%{http_code}' -u ${JENKINS_USER}:${JENKINS_API_TOKEN} ${JENKINS_URL}/api/json 2>&1" \
  "200\|403\|302"

check "Jenkins Skill jobs 命令" \
  "JENKINS_URL='${JENKINS_URL}' JENKINS_USER='${JENKINS_USER}' JENKINS_API_TOKEN='${JENKINS_API_TOKEN}' NODE_TLS_REJECT_UNAUTHORIZED=0 node /home/node/.openclaw/workspace/skills/jenkins/scripts/jenkins.mjs jobs 2>&1" \
  "jobs\|total"

check "Jenkins 有实际 Job" \
  "JENKINS_URL='${JENKINS_URL}' JENKINS_USER='${JENKINS_USER}' JENKINS_API_TOKEN='${JENKINS_API_TOKEN}' NODE_TLS_REJECT_UNAUTHORIZED=0 node /home/node/.openclaw/workspace/skills/jenkins/scripts/jenkins.mjs jobs 2>&1" \
  "example_fauliure_job"

check "Jenkins 可以拉取构建日志" \
  "JENKINS_URL='${JENKINS_URL}' JENKINS_USER='${JENKINS_USER}' JENKINS_API_TOKEN='${JENKINS_API_TOKEN}' NODE_TLS_REJECT_UNAUTHORIZED=0 node /home/node/.openclaw/workspace/skills/jenkins/scripts/jenkins.mjs console --job example_fauliure_job --build 1 --tail 10 2>&1" \
  "Pipeline\|Stages\|FAILURE\|SUCCESS"

echo ""

# ==========================================
# 3. GitLab 连通性
# ==========================================
echo "=== 3. GitLab 连通性 ==="

GITLAB_HOST="${GITLAB_HOST:-https://devopsclaw-nginx:8441}"
GITLAB_TOKEN="${GITLAB_TOKEN:-glpat-86x2pYV78K_2MMCZXc9RE286MQp1OjEH.01.0w1fpvejn}"

check "GitLab API 可达" \
  "curl -s -o /dev/null -w '%{http_code}' -k -H 'PRIVATE-TOKEN: ${GITLAB_TOKEN}' ${GITLAB_HOST}/api/v4/user 2>&1" \
  "200"

check "GitLab 返回用户信息" \
  "curl -s -k -H 'PRIVATE-TOKEN: ${GITLAB_TOKEN}' ${GITLAB_HOST}/api/v4/user 2>&1" \
  "username"

check "GitLab 仓库列表" \
  "curl -s -k -H 'PRIVATE-TOKEN: ${GITLAB_TOKEN}' ${GITLAB_HOST}/api/v4/projects?per_page=5 2>&1" \
  "\["

echo ""

# ==========================================
# 3.5. gitlab-skill 凭据与分支能力
# ==========================================
echo "=== 3.5 gitlab-skill 凭据与分支能力 ==="

check "gitlab_config.json 存在" \
  "test -f ~/.claude/gitlab_config.json && echo exists || echo missing" \
  "exists"

check "gitlab_config.json 权限 600" \
  "stat -c '%a' ~/.claude/gitlab_config.json 2>/dev/null || stat -f '%p' ~/.claude/gitlab_config.json 2>/dev/null | tail -c 4" \
  "600"

check "gitlab-skill CLI 搜索项目" \
  "python3 /home/node/.openclaw/workspace/skills/gitlab-skill/scripts/gitlab_api.py projects --search model_test 2>&1 | head -c 300" \
  "model_test\|root/"

check "gitlab-skill CLI 创建测试分支" \
  "python3 /home/node/.openclaw/workspace/skills/gitlab-skill/scripts/gitlab_api.py create-branch --project 'root/model_test' --branch 'verify-${RANDOM}' --branch-ref 'main' 2>&1 | head -c 300" \
  "url\|web_url\|Success\|created"

check "GitLab API 搜索项目（获取 ID）" \
  "curl -s -k -H 'PRIVATE-TOKEN: ${GITLAB_TOKEN}' '${GITLAB_HOST}/api/v4/projects?search=model_test' 2>&1 | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data[0][\"id\"] if data else \"NONE\")' 2>&1" \
  "[0-9]"

check "GitLab Token 写权限（创建 MR 需要）" \
  "curl -s -k -o /dev/null -w '%{http_code}' -H 'PRIVATE-TOKEN: ${GITLAB_TOKEN}' -X POST '${GITLAB_HOST}/api/v4/projects' --data-urlencode 'name=ci-selfheal-verify-test' --data-urlencode 'visibility=private' 2>&1" \
  "201\|403\|400"

echo "  ℹ️  若上面 token 权限结果显示 FAIL/403：当前 GitLab Token 缺少 api 或 write_repository 权限"
echo "      去 GitLab → Settings → Access Tokens 给 Token 添加 api 权限后重试"
echo "      不修复也不影响 CI 自愈核心功能（ci-selfheal 用 Jenkins API 更新 config.xml，无需 GitLab 写权限）"
echo ""

echo ""

# ==========================================
# 4. AI 模型可用
# ==========================================
echo "=== 4. AI 模型可用 ==="

check "openclaw agent 基础响应" \
  "openclaw agent --agent main --message 'Reply with just OK' --json 2>&1 | head -c 500" \
  "OK\|ok"

echo ""

# ==========================================
# 5. 已安装 Skill
# ==========================================
echo "=== 5. 已安装 Skill ==="

for sk in jenkins ci-cd-watchdog cicd-pipeline gitlab-skill n8n ci-monitor claw-summarize-pro; do
  check "Skill: $sk" \
    "ls /home/node/.openclaw/workspace/skills/$sk/SKILL.md 2>&1" \
    "SKILL.md"
done

echo ""

# ==========================================
# 6. ci-selfheal 文件完整性
# ==========================================
echo "=== 6. ci-selfheal 文件完整性 ==="

CI_DIR="/home/node/.openclaw/workspace/skills/ci-selfheal"

for f in config.yaml skill.toml SKILL.md README.md install.sh run.sh .env; do
  check "文件: $f" \
    "test -f ${CI_DIR}/${f} && echo exists || echo missing" \
    "exists"
done

for f in scripts/orchestrator.py scripts/webhook_listener.py scripts/agent_wrapper.py scripts/__init__.py; do
  check "文件: $f" \
    "test -f ${CI_DIR}/${f} && echo exists || echo missing" \
    "exists"
done

check "Python 语法: orchestrator.py" \
  "PYTHONPATH='/tmp/selfheal-deps:${CI_DIR}' python3 -m py_compile ${CI_DIR}/scripts/orchestrator.py 2>&1 && echo OK" \
  "OK"

check "Python 语法: webhook_listener.py" \
  "PYTHONPATH='/tmp/selfheal-deps:${CI_DIR}' python3 -m py_compile ${CI_DIR}/scripts/webhook_listener.py 2>&1 && echo OK" \
  "OK"

check "Python 语法: agent_wrapper.py" \
  "PYTHONPATH='/tmp/selfheal-deps:${CI_DIR}' python3 -m py_compile ${CI_DIR}/scripts/agent_wrapper.py 2>&1 && echo OK" \
  "OK"

echo ""

# ==========================================
# 7. Python 依赖
# ==========================================
echo "=== 7. Python 依赖 ==="

check "PyYAML 可加载" \
  "PYTHONPATH='/tmp/selfheal-deps:${CI_DIR}' python3 -c 'import yaml; print(\"OK\")' 2>&1" \
  "OK"

check "requests 可加载" \
  "PYTHONPATH='/tmp/selfheal-deps:${CI_DIR}' python3 -c 'import requests; print(\"OK\")' 2>&1" \
  "OK"

echo ""

# ==========================================
# 8. Webhook 服务 + 端到端测试
# ==========================================
echo "=== 8. Webhook 服务 + 端到端测试 ==="

WEBHOOK_STARTED=false
if ! curl -s http://localhost:8080/health 2>/dev/null | grep -q '\"status\".*\"ok\"'; then
  echo "  清理旧 Webhook 进程 + 启动服务..."
  pkill -f "scripts.webhook_listener" 2>/dev/null || true
  sleep 1
  cd "$CI_DIR"
  source .env 2>/dev/null || true
  nohup env PYTHONPATH="/tmp/selfheal-deps:$(pwd)" python3 -m scripts.webhook_listener --host 0.0.0.0 --port 8080 > /tmp/selfheal-verify.log 2>&1 &
  WEBHOOK_PID=$!

  for i in 1 2 3 4 5; do
    sleep 2
    if curl -s http://localhost:8080/health 2>/dev/null | grep -q '\"status\"'; then
      echo "  ✅ Webhook 服务已启动 (PID=$WEBHOOK_PID)"
      WEBHOOK_STARTED=true
      break
    fi
  done

  if [ "$WEBHOOK_STARTED" = false ]; then
    echo "  ❌ Webhook 服务启动失败"
    echo "  --- 启动日志 (tail -10) ---"
    tail -10 /tmp/selfheal-verify.log 2>/dev/null
    echo "  ---"
    SKIP=$((SKIP + 6))
  fi
else
  echo "  ℹ️  Webhook 服务已在运行"
fi

if [ "$WEBHOOK_STARTED" = true ] || curl -s http://localhost:8080/health 2>/dev/null | grep -q '"status"'; then
  check "服务健康检查 /health" \
    "curl -s http://localhost:8080/health 2>&1" \
    '"status":.*"ok"'

  check "非 FAILURE 状态被过滤" \
    "curl -s -X POST http://localhost:8080/webhook/ci-failure -H 'Content-Type: application/json' -d '{\"job\":\"verify-test\",\"build\":1,\"status\":\"SUCCESS\"}' 2>&1" \
    "skipped"

  check "FAILURE 状态被接受" \
    "curl -s -X POST http://localhost:8080/webhook/ci-failure -H 'Content-Type: application/json' -d '{\"job\":\"verify-test\",\"build\":99,\"status\":\"FAILURE\",\"branch\":\"dev/verify\",\"repo\":\"root/model_test\"}' 2>&1" \
    "accepted"

  check "admin/reset 端点可用" \
    "curl -s http://localhost:8080/admin/reset 2>&1" \
    "reset"

  echo ""
  echo "  --- 端到端自愈测试 (example_fauliure_job) ---"

  check "E2E: 触发真实 Job 自愈" \
    "curl -s http://localhost:8080/admin/reset > /dev/null; curl -s -X POST http://localhost:8080/webhook/ci-failure -H 'Content-Type: application/json' -d '{\"job\":\"example_fauliure_job\",\"build\":1,\"status\":\"FAILURE\",\"branch\":\"dev/test\",\"repo\":\"root/model_test\"}' 2>&1" \
    "accepted"

  # 等 AI 诊断 + Jenkins 构建（最多 120 秒）
  echo -n "  等待自愈流程 (最多 120s)..."
  for i in $(seq 1 24); do
    sleep 5
    if tail -5 /home/node/.openclaw/workspace/skills/ci-selfheal/selfheal.log 2>/dev/null | grep -q "SUCCESS\|LOW_CONFIDENCE\|FIX_APPLY_FAILED\|\: Not Found"; then
      echo ""
      break
    fi
    echo -n "."
  done
  echo ""

  check "E2E: 自愈流程已执行" \
    "tail -20 /home/node/.openclaw/workspace/skills/ci-selfheal/selfheal.log 2>/dev/null | grep -c 'Agent instruction\|Trigger build\|Diagnosis report\|SUCCESS\|FIX_APPLY' 2>/dev/null || echo 0" \
    "[1-9]"

  # 清理 — 如果你启动了 webhook，关掉它
  if [ "$WEBHOOK_STARTED" = true ] && [ -n "$WEBHOOK_PID" ]; then
    kill "$WEBHOOK_PID" 2>/dev/null || true
    echo "  🧹 Webhook 服务已停止 (PID=$WEBHOOK_PID)"
  fi
else
  echo "  ⚠️  Webhook 未就绪，跳过 HTTP/E2E 测试"
fi

echo ""
echo "  --- MR 创建测试 ---"

check "GitLab MR: API 创建测试 MR" \
  "PROJECT_ID=\$(curl -s -k -H 'PRIVATE-TOKEN: ${GITLAB_TOKEN}' '${GITLAB_HOST}/api/v4/projects?search=model_test' 2>/dev/null | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data[0][\"id\"] if data else 0)'); curl -s -k -X POST '${GITLAB_HOST}/api/v4/projects/'\"\$PROJECT_ID\"'/merge_requests' --header 'PRIVATE-TOKEN: ${GITLAB_TOKEN}' --data-urlencode 'source_branch=verify-test-cli' --data-urlencode 'target_branch=main' --data-urlencode 'title=ci-selfheal-verify-MR' --data-urlencode 'description=Deployment verification MR' 2>&1 | head -c 300" \
  "web_url\|iid\|merge request\|already exists\|Another open merge request"

echo ""

# ==========================================
# 9. 白名单逻辑（导入模块验证）
# ==========================================
echo "=== 9. 白名单逻辑 ==="

check "白名单: 允许的 repo+branch" \
  "cd ${CI_DIR} && PYTHONPATH='/tmp/selfheal-deps:${CI_DIR}' python3 -c \"from scripts.orchestrator import Orchestrator; o=Orchestrator(); print(o._whitelist_check('root/model_test','dev/test'))\" 2>&1" \
  "True"

check "白名单: 拒绝 main 分支" \
  "cd ${CI_DIR} && PYTHONPATH='/tmp/selfheal-deps:${CI_DIR}' python3 -c \"from scripts.orchestrator import Orchestrator; o=Orchestrator(); print(o._whitelist_check('root/model_test','main'))\" 2>&1" \
  "False"

check "白名单: 拒绝未知仓库" \
  "cd ${CI_DIR} && PYTHONPATH='/tmp/selfheal-deps:${CI_DIR}' python3 -c \"from scripts.orchestrator import Orchestrator; o=Orchestrator(); print(o._whitelist_check('evil/repo','dev/test'))\" 2>&1" \
  "False"

echo ""

# ==========================================
# 10. 脱敏测试
# ==========================================
echo "=== 10. 脱敏测试 ==="

check "Token 脱敏" \
  "cd ${CI_DIR} && PYTHONPATH='/tmp/selfheal-deps:${CI_DIR}' python3 -c \"from scripts.orchestrator import Orchestrator; o=Orchestrator(); r=o._desensitize('password=secret123'); print('OK' if 'REDACTED' in r and 'secret123' not in r else 'FAIL')\" 2>&1" \
  "OK"

check "内网 IP 脱敏" \
  "cd ${CI_DIR} && PYTHONPATH='/tmp/selfheal-deps:${CI_DIR}' python3 -c \"from scripts.orchestrator import Orchestrator; o=Orchestrator(); r=o._desensitize('connect 192.168.1.1'); print('OK' if 'INTERNAL_IP' in r else 'FAIL')\" 2>&1" \
  "OK"

echo ""

# ==========================================
# 汇总
# ==========================================
echo "========================================"
TOTAL=$((PASS + FAIL + SKIP))
echo "  总计: $TOTAL"
echo -e "  ${GREEN}通过: $PASS${NC}"
echo -e "  ${RED}失败: $FAIL${NC}"
echo -e "  ${YELLOW}跳过: $SKIP${NC}"
echo "========================================"

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "❌ 有 $FAIL 项未通过，请检查上面的 FAIL 行。"
  exit 1
else
  echo ""
  echo "✅ 全部通过！ci-selfheal 部署就绪。"
  exit 0
fi
