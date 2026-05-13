#!/bin/bash
# ci-selfheal 批量 Skill 安装脚本
# 运行环境：OpenClaw 容器内（docker exec -it devopsclaw-openclaw bash 后执行）
# 用法：bash install-all-skills.sh

set -e

SKILLS=(
  "jenkins"
  "ci-cd-watchdog"
  "cicd-pipeline"
  "gitlab-skill"
  "n8n"
  "ci-monitor"
  "claw-summarize-pro"
  "tkuehnl/agentic-devops"
  "lint"
  "security-auditor"
  "self-improve"
  "git-changelog"
  "tavily"
  "devops"
  "capability-evolver-pro"
  "docker"
)

echo "========================================"
echo "  ci-selfheal 依赖 Skill 一键安装"
echo "  共 ${#SKILLS[@]} 个 Skill"
echo "========================================"
echo ""

FAILED=()
for sk in "${SKILLS[@]}"; do
  echo "--- Installing: $sk ---"
  if openclaw skills install "$sk"; then
    echo "  ✅ $sk"
  else
    echo "  ⚠️  $sk failed (continuing...)"
    FAILED+=("$sk")
  fi
  echo ""
done

echo "========================================"
echo "  安装完成"
echo "  成功: $((${#SKILLS[@]} - ${#FAILED[@]})) / ${#SKILLS[@]}"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "  失败: ${FAILED[*]}"
fi
echo "========================================"
echo ""
echo "验证安装："
echo "  ls /home/node/.openclaw/workspace/skills/"
