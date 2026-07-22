# Token Saver

![Token Saver 的 Lite 与 Max 跨模型编排](media/og.png)

[English](README.md) | **简体中文**

Token Saver 是 Claude Code 与 Codex 共用的模型无关编排协议。它不会替你选择
对话模型：你进入会话时选定的模型始终是主循环。Token Saver 只决定规划、执行、
闸门、评审与集成如何分工。

项目地址：<https://github.com/vincemakes/token-saver>

## 你是否应该使用它？

适合大型、构建型、能提前写清验收标准的任务，例如多文件迁移、重复性改造和新
子系统。此时可以把实现交给执行模型 / Worker，同时让高级模型把 token 花在规划
与评审上。

小改动、纯讨论、尚未定位根因的调试，以及无法先写成规格的设计或安全决策，不适合
编排。Token Saver 会让开，由已经选定的主循环直接处理。

## Lite 与 Max 一览

Lite 和 Max 描述的是权威裁决放在哪里，不是模型品牌或质量排名。

```text
Lite（主循环内裁决）
高级模型主循环 ──规划 / AUTHORITY_PLAN_CHECK / AUTHORITY_FINAL_CHECK──> Worker

Max（外部高级裁决）
权威评审模型 <──AUTHORITY_PLAN_CHECK / AUTHORITY_FINAL_CHECK──> 主循环
                                                            └── 可选 Worker
```

- Lite：主循环负责思考、规划、主循环评审与两次权威裁决；Worker 负责被界定好的
  实现。没有合适 Worker 时也可以由主循环实现。
- Max：主循环负责协调、草拟计划和自己的代码评审；一个 canonical fingerprint
  不同的高级评审模型负责计划与最终裁决。更低成本的 Worker 是可选的，所以 Max
  可以是两层，也可以是三层。

## 主循环已经选定

主循环（main loop）在 Token Saver 启动前已经由宿主选定，并在整个运行中不可变。Profile、用户
配置、项目配置和单次参数都不能替换它，只能配置新派生的 Reviewer、Worker、Scout
或 Mechanic。

模型身份必须来自用户明确说明或宿主的结构化元数据。命令名、wrapper 名、账号名和
Endpoint 都不是模型身份。身份不清时返回 `needs_context`；显式 Max 没有可验证的
高级 Reviewer 时返回 `reviewer_unavailable`，绝不偷偷降级成 Lite。

## 共享状态机如何工作

Claude Code、Codex 与外部 CLI 走完全相同的状态序列：

```text
RESOLVE -> PREFLIGHT -> CLASSIFY -> RECON -> DRAFT_PLAN -> AUTHORITY_PLAN_CHECK -> DISPATCH -> GATE -> PATCH_AUDIT -> MAIN_LOOP_REVIEW -> AUTHORITY_FINAL_CHECK -> INTEGRATE
```

对于密封的外部 Worker 调用，所选拓扑也是运行时不变量：必填的
`worker --mode lite|max` 会作为 `authority_mode` 密封进 bundle。这个
`authority_mode` 在 review 与 integrate 阶段都不能切换、降级或重新解释。Lite bundle
只接受主循环 inline 裁决；Max bundle 只接受 fingerprint 不同的外部 Reviewer。

Worker 最多自修三次。最终 Reviewer 最多提出两轮 `revise`；第三次返回
`review_revise` 并停止。审批绑定
`source_snapshot_hash`、`worker_delta_hash` 与
`projected_task_patch_hash`，集成前任一内容变化都会让旧审批失效。

完整协议见 [SKILL.md](SKILL.md)、[协议参考](references/protocol.md) 与
[路由规则](references/routing.md)。

## 模型 Profile，而非模型锁定

内置 Profile 只是能力别名与默认偏好，不是状态机分支：

- Fable/Opus 主循环 + 更低成本 Claude Worker：Lite 示例。
- Sol 主循环 + Terra/Luna Worker：Lite 示例。
- Terra 主循环 + fingerprint 不同的 Sol Reviewer + 可选 Luna Worker：Max 示例。
- Kimi K3 只有在精确身份被固定并由 preflight 验证后，才能作为高级外部路由。

你可以增加未来模型或自定义 CLI 路由，只要它们声明能力和角色，并通过相同的身份、
权限、沙箱与证据检查。Profile 文件位于 [references/profiles](references/profiles)。

运行时 CLI 需要 Python 3.11+ 与 Git；POSIX 安装示例还会使用 `bash` 和 `install`。
可写的外部 Worker 还必须有验证过的 OS 后端：macOS 使用
`/usr/bin/sandbox-exec`，Linux（包括 WSL）使用 Bubblewrap（`bwrap`）。Windows
原生环境没有外部写 Worker 后端，只使用 Claude Code 或 Codex 的宿主原生 Agent。

## Claude Code 安装

以下命令会同时安装 Skill 与默认 Anthropic 角色资产；这些角色不会替换主循环。

**POSIX，用户级：**

```bash
mkdir -p "$HOME/.claude/skills"
git clone https://github.com/vincemakes/token-saver.git "$HOME/.claude/skills/token-saver"
mkdir -p "$HOME/.claude/agents"
for role in reviewer implementer mechanic scout; do
  install -m 0644 "$HOME/.claude/skills/token-saver/assets/agents/claude-code/$role.md" \
    "$HOME/.claude/agents/token-saver-$role.md"
done
```

**POSIX，项目级：**

```bash
mkdir -p .claude/skills
git clone https://github.com/vincemakes/token-saver.git .claude/skills/token-saver
mkdir -p .claude/agents
for role in reviewer implementer mechanic scout; do
  install -m 0644 ".claude/skills/token-saver/assets/agents/claude-code/$role.md" \
    ".claude/agents/token-saver-$role.md"
done
```

**PowerShell，用户级：**

```powershell
$skill = Join-Path $HOME ".claude\skills\token-saver"
$agents = Join-Path $HOME ".claude\agents"
New-Item -ItemType Directory -Force (Split-Path $skill -Parent) | Out-Null
git clone https://github.com/vincemakes/token-saver.git $skill
New-Item -ItemType Directory -Force $agents | Out-Null
foreach ($role in "reviewer", "implementer", "mechanic", "scout") {
  Copy-Item (Join-Path $skill "assets\agents\claude-code\$role.md") `
    (Join-Path $agents "token-saver-$role.md")
}
```

**PowerShell，项目级：**

```powershell
$skill = ".claude\skills\token-saver"
$agents = ".claude\agents"
New-Item -ItemType Directory -Force (Split-Path $skill -Parent) | Out-Null
git clone https://github.com/vincemakes/token-saver.git $skill
New-Item -ItemType Directory -Force $agents | Out-Null
foreach ($role in "reviewer", "implementer", "mechanic", "scout") {
  Copy-Item (Join-Path $skill "assets\agents\claude-code\$role.md") `
    (Join-Path $agents "token-saver-$role.md")
}
```

## Codex 安装

使用 Sol Profile 前先检查版本：

```bash
codex --version
```

`codex --version` 只用于诊断，不是能力证明。选择内置 Profile 前，Token Saver 的
preflight / 预检必须确认当前 Codex 支持自定义 Agent、当前账号与模型目录确实提供
所需的 Sol/Terra/Luna ID，并且目标沙箱与 reasoning 参数可用。可用性检查失败时返回
`provider_unavailable` 或 `reviewer_unavailable`；安装流程不会自动升级 Codex。

**POSIX，项目级：**

```bash
mkdir -p .agents/skills
git clone https://github.com/vincemakes/token-saver.git .agents/skills/token-saver
mkdir -p .codex/agents
for role in reviewer implementer mechanic scout; do
  install -m 0644 ".agents/skills/token-saver/assets/agents/codex/$role.toml" \
    ".codex/agents/token-saver-$role.toml"
done
```

**POSIX，用户级：**

```bash
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/vincemakes/token-saver.git "$HOME/.agents/skills/token-saver"
mkdir -p "$HOME/.codex/agents"
for role in reviewer implementer mechanic scout; do
  install -m 0644 "$HOME/.agents/skills/token-saver/assets/agents/codex/$role.toml" \
    "$HOME/.codex/agents/token-saver-$role.toml"
done
```

**PowerShell，项目级：**

```powershell
$skill = ".agents\skills\token-saver"
$agents = ".codex\agents"
New-Item -ItemType Directory -Force (Split-Path $skill -Parent) | Out-Null
git clone https://github.com/vincemakes/token-saver.git $skill
New-Item -ItemType Directory -Force $agents | Out-Null
foreach ($role in "reviewer", "implementer", "mechanic", "scout") {
  Copy-Item (Join-Path $skill "assets\agents\codex\$role.toml") `
    (Join-Path $agents "token-saver-$role.toml")
}
```

**PowerShell，用户级：**

```powershell
$skill = Join-Path $HOME ".agents\skills\token-saver"
$agents = Join-Path $HOME ".codex\agents"
New-Item -ItemType Directory -Force (Split-Path $skill -Parent) | Out-Null
git clone https://github.com/vincemakes/token-saver.git $skill
New-Item -ItemType Directory -Force $agents | Out-Null
foreach ($role in "reviewer", "implementer", "mechanic", "scout") {
  Copy-Item (Join-Path $skill "assets\agents\codex\$role.toml") `
    (Join-Path $agents "token-saver-$role.toml")
}
```

原生 Agent TOML 只是默认配置，不是安全边界。Max Reviewer 必须由运行时确认实际子
进程的 fingerprint 与最终生效的只读权限。详情见
[Codex 适配器](references/adapters/codex.md)。

## Kimi 与 GLM 外部路由

Codex 可以调用已有 `claude-kimi*` / `claude-glm*` 命令；这不代表 Kimi 会原生
出现在 Codex 模型选择器中。Command name is not model identity / 命令名不是模型身份。

从安装后的项目目录迁移现有 Provider 数据，并把 wrapper 安装到明确指定的目录：

```bash
bash scripts/setup-model-providers.sh --install-path "$HOME/.local/bin"
```

安装脚本不会修改 shell 启动文件；如果需要，请自行把该目录加入 `PATH`。
如果旧、新 credentials 文件都不存在，显式 `--install-path` 仍只安装 wrappers，
不会创建 credentials 文件或虚构秘密值。使用 Provider 路由前，请在启动时提供所需
环境变量，或另行配置 credentials。

| 路由角色 | Reviewer transport 基础命令 | 只有验证过 OS 沙箱才允许的写命令 |
|---|---|---|
| Kimi Reviewer candidate | `claude-kimi` | — |
| Kimi implementer | — | `claude-kimi-bypass -p` |
| GLM Reviewer candidate | `claude-glm` | — |
| GLM implementer | — | `claude-glm-bypass -p` |
| GLM fast scout/mechanic | `claude-glm-turbo` | `claude-glm-turbo-bypass -p` |

当新的 credentials 文件还不存在时，setup 会把现有
`$HOME/.claude/fable-token-saver/providers.env` 当作数据迁移，绝不会 source 它。
主循环随后可用一次密封调用完成外部实现：

```bash
mkdir -p "$PWD/../token-saver-runs"
python3 scripts/token-saver-route.py worker \
  --repo "$PWD" \
  --temp-parent "$PWD/../token-saver-runs" \
  --route claude-kimi-bypass \
  --task /absolute/path/to/task.json \
  --mode lite
```

该命令会创建并物化一次性 worktree、重建 manifest、重新探测沙箱，并且只在探测
成功后注入 bypass 权限；随后运行 Worker 与 gates，再密封 delta。它不会自动集成。
已经选定的主循环负责思考与最终评审、次级 Worker 负责实现时使用 `--mode lite`。
较低一级模型作为主循环、另一个更高级且 fingerprint 不同的外部 Reviewer 负责权威
裁决时使用 `--mode max`；Max 仍可把实现交给更低一级 Worker。同一 manifest 的模式
不可改变；需要改变拓扑时必须新建 Worker 调用。

主循环检查完整证据并写好严格的 review context JSON 后，执行与密封模式匹配的评审。
Lite 由继承的主循环 inline 裁决：

```bash
python3 scripts/token-saver-route.py review --inline \
  --main-fingerprint <provider:model:variant> \
  --manifest <manifest> \
  --context /absolute/path/to/review-context.json
```

Max 使用 profile 与 route 指定的外部 Reviewer；此时 `--inline` 无效：

```bash
python3 scripts/token-saver-route.py review --profile /absolute/path/to/profile.json \
  --route <reviewer-route> \
  --main-fingerprint <provider:model:variant> \
  --manifest <manifest> \
  --context /absolute/path/to/review-context.json
```

评审通过后，运行时会写入绑定三哈希和本次 invocation 的最终评审 receipt。integrate
通过 manifest 读取这份密封 receipt，不再接受调用者另传 approval 文件：

```bash
python3 scripts/token-saver-route.py integrate <manifest>
```

精确 task 与 review context schema 见
[外部 CLI 安全合同](references/adapters/external-cli.md)。不要在普通仓库里直接运行
bypass alias；缺少 one-shot invocation manifest 时会安全拒绝。同一套 manifest 与
命令合同可由 Claude Code 或 Codex 主循环驱动；模型与 Provider 名只是 route 数据，
不是工作流分支。

普通 wrapper 本身并非只读。Reviewer transport 会追加
`--safe-mode --no-session-persistence --permission-mode plan --tools "" -p`，在隔离的
证据目录中运行，只从 stdin 接收 packet，并验证目录没有变化；在精确 fingerprint
验证前仍不能成为 Max Reviewer。

Bypass wrapper 只会在一次性 worktree 与验证过的 OS 沙箱中运行，绝不直接接触用户
仓库。当前外部写 Worker 的验证后端是 macOS 与 Linux（包括 WSL 中的 Linux）。
Windows 原生外部写路由会返回 `sandbox_unavailable` 并拒绝启动；Claude Code/Codex
原生 Agent 编排仍可使用。安装 wrapper 也不会让 Kimi/GLM 变成 Codex 原生模型。

外部 Worker 模型只获得 `Read`、`Glob`、`Grep`、`Edit` 与 `Write` 工具。Bash 已禁用，
Web 与 MCP 工具不可用。task 声明的 gate 命令使用直接参数数组，由 Token Saver 宿主
在模型调用后运行，不是授予模型的 shell 权限。

## 安全与失败行为

- Reviewer 只收到完整的 canonical evidence packet，不收到仓库、工具或凭据。
- 外部 Worker 只能写一次性 worktree 与本次调用的 state 目录。
- Prompt、日志、manifest 与 review packet 都不包含凭据值，但 Provider 客户端进程
  仍会获得连接 Endpoint 所需的凭据。应优先使用短期、窄权限 token，并限制到 route
  所需的最小权限。工具 allowlist 与文件系统沙箱不是网络安全边界：恶意或被入侵的
  Provider 二进制可以滥用它能读取的数据或收到的凭据，Token Saver 无法阻止它通过
  被允许的 Provider 网络连接发送这些内容。因此只应安装并运行可信的 Provider 二进制。
- 审批绑定三哈希；目标内容漂移即返回 `destination_changed` 并要求重新快照与审批。
- Token Saver 不 stash、reset、覆盖用户改动，也不靠 fuzzy apply 绕过冲突。
- 公开状态固定为 `ok`、`needs_context`、`gate_failed`、
  `provider_unavailable`、`reviewer_unavailable`、`timeout`、`scope_violation`、
  `transport_error`、`review_revise`、`approval_stale`、`destination_changed` 与
  `sandbox_unavailable`。

外部 CLI 细节见 [安全合同](references/adapters/external-cli.md)。

## 参考基准快照

现有数字来自历史 Claude/Fable/Opus 参考栈，不能预测 Sol、Kimi 或未来 Profile 的
节省比例。`-42%/-89%` 是当时记录的最强模型输出 token 变化，`-34%/-88%` 是报告
使用的价格加权额度代理。盲测 bug-hunt 只是一次观察，不是普遍性证明。

原始数字、方法与限制完整保留在
[BENCHMARKS.zh-CN.md](BENCHMARKS.zh-CN.md)。

## Token Saver 何时让开

以下情况由主循环直接处理，不开启编排：小于任务包/评审开销的改动；纯分析；尚未
定位根因的具体错误；安全或架构核心仍需要探索；用户只问模型价格/选择；或者可执行
规格比代码本身更长。

## 许可证

[MIT](LICENSE)
