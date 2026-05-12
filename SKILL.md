# ci-selfheal

全自动 Jenkins 构建失败自愈闭环。

## 流程

1. 监听 Webhook（POST /webhook/ci-failure）
2. G0 白名单校验（仓库 + 分支）
3. 拉取日志并脱敏 → AI 诊断 → 生成修复
4. 提交修复到当前分支并推送
5. 触发 Jenkins 重建并轮询结果
6. 成功则通知；失败重试最多 5 次，熔断后通知人工

## 依赖 Skill

| Skill | 用途 |
|-------|------|
| `jenkins` | 获取日志、触发构建、查询状态 |
| `ci-cd-watchdog` | 日志解析、根因定位 |
| `cicd-pipeline` | CI/CD 流程管理 |
| `gitlab-skill` | 代码提交与推送 |
| `n8n` | 通知推送 |

## 使用方式

```bash
# 启动 Webhook 监听服务
./run.sh

# 或通过 CLI 手动触发
python3 bin/ci-selfheal orchestrate --job <job> --build <N> --branch <branch> --repo <repo>

# 查看自愈状态
python3 bin/ci-selfheal status --job <job>
```

## 配置

编辑 `config.yaml` 设置 Jenkins/GitLab 地址、白名单、重试次数等参数。
在 `.env` 中设置 `JENKINS_API_TOKEN`、`GITLAB_HOST`、`GITLAB_TOKEN`。
