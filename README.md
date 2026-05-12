# openclaw-skill-ci-selfheal

Jenkins CI 构建失败全自动自愈闭环。

When a Jenkins build fails, this Skill automatically:
1. **White-list check** (G0 gate) — repo + branch must be whitelisted
2. **Collects** build logs (via `jenkins` Skill)
3. **Diagnoses** root cause (via `ci-cd-watchdog` + AI agent)
4. **Generates** fix and applies it to a new branch (via `gitlab-skill`)
5. **Triggers** Jenkins rebuild and polls for success
6. **Creates** Merge Request on success; retries up to 5 times on failure
7. **Notifies** via `n8n` on circuit break

## 依赖 Skill

| Skill | 在闭环中的角色 |
|-------|-------------|
| `jenkins` | 拉取构建日志、触发构建、查询构建状态 |
| `ci-cd-watchdog` | 解析 Jenkins 日志、定位根因、输出修复建议 |
| `cicd-pipeline` | CI/CD 流程管理、触发重建 |
| `gitlab-skill` | 创建 fix 分支、提交修复代码、创建 Merge Request |
| `n8n` | 钉钉/邮件 通知推送 |

## 核心概念

### 熔断（Circuit Breaker）

**熔断**是一种保护机制：当同一个 Job 连续 5 次自愈失败后，系统判定"AI 修不好这个问题"，自动停止对该 Job 的任何自动修复操作。

设计目的：避免在同一个修不好的问题上反复浪费 AI API Token 和 CPU 资源。

### 冷却期（Cooldown Period）

熔断触发后，该 Job 进入 **2 小时冷却期**。期间收到的新失败事件会被直接丢弃（仅记录日志，不触发自愈流程）。冷却期结束后自动解除，不再需要人工介入。

### 为什么需要熔断？

```
Job 失败 → AI 诊断（正常）
       → 修复失败 → 重试 1（正常）
       → 失败 → 重试 2
       → 失败 → 重试 3
       → 失败 → 重试 4
       → 失败 → 重试 5
       → 🔥 熔断生效，进入 2 小时冷却期
       → 冷却期内新失败事件 → 跳过
       → 2 小时后自动解除
```

没有熔断的话，同一个修不好的错误会让 AI 无限循环调用，既烧钱又没结果。

### 手动清除熔断

**方式 A：HTTP 端点（推荐，无需重启）**

```bash
curl http://localhost:8080/admin/reset
# 返回: {"status":"reset","message":"state cleared, all circuit breakers removed"}
```

**方式 B：命令行（需重启）**

```bash
pkill -f "scripts.webhook_listener"
cd /home/node/.openclaw/workspace/skills/ci-selfheal
echo '{"version":"2.0.0","chains":{},"circuit_breaker":{}}' > .self-heal-state.json
# 然后重新启动服务
```

## Quick Start

```bash
./install.sh
./run.sh
```

## Manual Usage

```bash
python3 bin/ci-selfheal serve
python3 bin/ci-selfheal orchestrate --job example-pipeline --build 42 --branch dev --repo root/model_test
python3 bin/ci-selfheal status --job example-pipeline
```

## Configuration

Edit `config.yaml` and set required environment variables in `.env`.

## 重启服务三步走（清状态顺序很重要）

```bash
# ① 先杀进程
pkill -f "scripts.webhook_listener"

# ② 再清状态（进程已死，清文件安全）
echo '{"version":"2.0.0","chains":{},"circuit_breaker":{}}' > .self-heal-state.json

# ③ 最后启动（新进程读的是干净状态）
source .env
nohup env PYTHONPATH="/tmp/selfheal-deps:$(pwd)" python3 -m scripts.webhook_listener --host 0.0.0.0 --port 8080 > selfheal.log 2>&1 &
```

> ⚠️ **为什么不能在启动后清状态？** `Orchestrator` 在 `__init__` 时就把 `.self-heal-state.json` 读到内存字典 `self.state` 里了。启动后再清文件，内存里的旧状态还在。
