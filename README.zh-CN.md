# Model Boss

![Model Boss 的 Lite 与 Max 跨模型编排](media/og.png)

[English](README.md) | **简体中文**

[![tests](https://github.com/vincemakes/model-boss/actions/workflows/tests.yml/badge.svg)](https://github.com/vincemakes/model-boss/actions/workflows/tests.yml)

Big models think. Small models ship.

**Cross-model coding orchestration**（跨模型编程编排），适用于 Claude Code 与 Codex。对话的主循环由宿主选定并沿用；Model Boss 永远不会替换它。**Boss** 是工作流的权威持有者：Lite 由继承的主循环 inline 持有权威，Max 由一个独立且已验证的 Reviewer 持有权威。“大/小”是相对于工作流的角色，并非 Provider 或模型的通用排名。

项目地址：<https://github.com/vincemakes/model-boss>

**一个仓，两个 Skill。** 安装一次，之后直接说你要什么——由 skill 判断上下文。

| Skill | 说这句话 | 做什么 |
|---|---|---|
| `boss-dispatch` | `让 opus 去写，你来审` · `走 model boss max` · `省token` · `分层干活` | 规划、派发 Worker、过闸并审计证据，权威始终留在继承的主循环；这就是今天的 Model Boss 编排 Skill，原 skill 名 `model-boss`。 |
| `boss-call` | `看看三个终端做得怎样` · `给 reelfo 发指令` | 一个 Boss 与多个成员会话之间的单线信箱；成员每轮开头汇报。 |

主 skill 从仓根搬进 `boss-dispatch/`，skill 名从 `model-boss` 改成 `boss-dispatch`；触发词不变，更新后重新运行 `bash boss-dispatch/install.sh` 即可。

## 用法

安装完成后（见下方 Claude Code / Codex 安装小节）不需要运行任何命令——直接在对话里说。`model boss`、`省token`、`分层干活` 这类短语会触发 Skill，然后描述你想要的拓扑：

```text
走 model boss,让 sonnet 实现 src/http 的重试逻辑,你来审核        → Lite
走 model boss,让 opus 去开发,你来审核                          → Lite，`opus` = 最新的 Opus
走 model boss,让 opus 4.6 去开发,你来审核                      → Lite，Worker 钉在 Opus 4.6
model boss max — have fable review the plan and the final diff → Max
走 model boss max,让 fable 审计划和最终 diff                    → Max
```

口述的模型名精确匹配对应路由；目录里没有的版本（`fable 6`）以 `needs_context` 停下，不会猜一个相近版本。

Lite 由继承的主循环持有两个权威检查点并派遣可选 Worker；Max 需要一个身份独立且验证过的 Reviewer 在派发前批准计划、在集成前批准最终证据——显式 Max 找不到合格 Reviewer 时以 `reviewer_unavailable` 停止，绝不静默降级。

开始任何工作前，Model Boss 会先打印解析后的拓扑（`Main loop / Resolved mode / Authority / Worker / Resolution source`），每行都是 `route/model@effort`。务必看一眼：Worker/Reviewer 名字只是路由别名，verdict 里显示的才是别名在你宿主上实际解析到的精确模型和运行 effort——例如光说 `opus` 解析为宿主暴露的最新 Opus，宿主未更新时可能落后于最新公开版本。命令名/别名永远不是模型身份证明。verdict 下面会接着打印 `estimate` 表，说明这次任务 inline、Lite、Max 各要花多少。

小改动、纯讨论、未定位根因的调试不会触发编排：Skill 让开，主循环正常工作。

## 你是否应该使用它？

适合大型、构建型、能提前写清验收标准的任务，例如多文件迁移、重复性改造和新
子系统。此时可以把实现交给执行模型 / Worker，同时让高级模型把 token 花在规划
与评审上。

小改动、纯讨论、尚未定位根因的调试，以及无法先写成规格的设计或安全决策，不适合
编排。Model Boss 会让开，由已经选定的主循环直接处理。

Claude Max 订阅的每周额度是一个共用总池，外加 Fable 最多用一半的上限，所以 Model Boss 的默认策略是
「节奏内选最强」，不是省钱。大量重构、新建子系统、多功能并行开发这类有实质执行体量、验收标准写得清的
任务走 Lite：Fable 主循环负责计划、评审、集成，Opus 5 worker 开 `xhigh` 负责实现，独立的包并行派。
这个拓扑实测 Fable 花费占总花费 48%，两个半区会同时见底。四种情况不走它：

| 情况 | 为什么 | 该用什么 |
|---|---|---|
| 判断密集：定位根因、设计取舍、安全决策 | 推理本身就是工作量，交接只增加等待和读 diff 的成本，结果相同 | Fable 自己干，effort 开 `high` |
| 单个包、改动小于约 200 行 | 任务包加评审花的 Fable 超过直接改；交接是串行的 | Fable 自己干 |
| 只有一条流、你在等结果 | 单 worker 是串行的一跳（实测慢 2.7 倍），并行优势要两个以上的包才有 | 能拆就拆成包，拆不了就自己干 |
| Fable 半区快于进度 | Lite 每任务花的 Fable 和自己干差不多，它填另一半，不撑这一半 | 另开 Max 会话：Opus 5 主循环，Fable 只审两个检查点，每任务约 $0.4 Fable |

`estimate` 命令编码的就是这套规则。给它任务形状（`--lines`、`--files`、`--judgment`、`--packets`）和 /usage 里的三个百分比
（`--total-used`、`--fable-used`、`--week-elapsed`），它会打印所处的节奏状态、成本表和建议，Fable 半区见底时给出 `switch-main-loop`。
一周只盯一个数：Fable 占总花费的比例，目标 50%；判断类工作自己干会把它推高，Max 或 Opus 会话把它拉回。

2026-09 的 Fable 5.1 复跑（从 [BENCHMARKS.zh-CN.md](BENCHMARKS.zh-CN.md) 链接过去）在同一大型任务上实测：Fable 5.1 开 low
自己干是最便宜也最快的 Fable 路径；Fable 主循环配 Opus 5 worker 的 Fable 花费与自己干相当，同时把另一半额度用起来；
配 Sonnet 5 worker 省约三分之一 Fable。派工是把整周额度用满的办法，Max 是撑 Fable 的办法，低 effort 是让 Fable 变便宜的办法。

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

主循环（main loop）在 Model Boss 启动前已经由宿主选定，并在整个运行中不可变。Profile、用户
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

完整协议见 [SKILL.md](boss-dispatch/SKILL.md)、[协议参考](boss-dispatch/references/protocol.md) 与
[路由规则](boss-dispatch/references/routing.md)。

## 模型 Profile，而非模型锁定

内置 Profile 只是能力别名与默认偏好，不是状态机分支：

- Fable/Opus 主循环 + 更低成本 Claude Worker：Lite 示例。
- Sol 主循环 + Terra/Luna Worker：Lite 示例。
- Terra 主循环 + fingerprint 不同的 Sol Reviewer + 可选 Luna Worker：Max 示例。
- Kimi K3 只有在精确身份被固定并由 preflight 验证后，才能作为高级外部路由。

默认 Anthropic Profile 钉死了 Claude Code 选择器今天暴露的全部模型（目录快照 2026-09-02），每个高级模型各有一条只读 Reviewer 路由和一条可写 Worker 路由：

| 路由 | 精确模型 ID | Effort 档位 | 缓存读价 $/MTok | 最小可缓存前缀 |
|---|---|---|---|---|
| `fable-5.1`、`fable-5.1-worker` | `claude-fable-5-1` | low…max | 0.25 | 512 |
| `fable-5`、`fable-5-worker` | `claude-fable-5` | low…max | 1.00 | 512 |
| `opus-5`、`opus-5-worker` | `claude-opus-5` | low…max | 0.50 | 512 |
| `opus-4.8`、`opus-4.8-worker` | `claude-opus-4-8` | low…max | 0.50 | 1024 |
| `opus-4.7`、`opus-4.7-worker` | `claude-opus-4-7` | low…max | 0.50 | 2048 |
| `opus-4.6`、`opus-4.6-worker` | `claude-opus-4-6` | low、medium、high、max | 0.50 | 4096 |
| `sonnet-5` | `claude-sonnet-5` | low…max | 0.20 | 1024 |
| `sonnet-4.6` | `claude-sonnet-4-6` | low、medium、high、max | 0.30 | 1024 |
| `haiku-4.5` | `claude-haiku-4-5` | 不支持 | 0.10 | 4096 |

默认偏好：Reviewer 先 `fable-5.1` 后 `opus-5`，effort `high`；Worker 先 `opus-5-worker` 后 `sonnet-5`，effort `xhigh`；Scout 与 Mechanic 用 `haiku-4.5`。每条路由带 `effort`（按目录校验，Opus 4.6 上写 `xhigh` 是配置错误）、`quota_weight`（默认 `1.0`，哪个窗口最紧就把它调大）和可选的口语 `aliases`。effort 是花费控制，不是身份：同一模型两个 effort 在权威分离上仍视为同一个模型。两个辅助命令都不调用任何模型：

```bash
python3 <model-boss-skill-root>/scripts/model-boss.py match-models --profile anthropic --text "让 opus 4.6 去开发"
python3 <model-boss-skill-root>/scripts/model-boss.py estimate --profile anthropic --main-model claude-fable-5-1 --main-effort medium --worker opus-5-worker --lines 800 --files 8 --judgment low --packets 2 --total-used 40 --fable-used 45 --week-elapsed 40
```

`match-models` 按最长匹配把请求里的模型名映射到路由，目录里没有的版本（`fable 6`）返回 `needs_context` 而不是猜一个相近版本。`estimate` 按任务形状给 inline、Lite、Max 三种方案算账，计入 Worker 冷启动（缓存按模型隔离，子代理读不到主循环的缓存）、派工后的产出体积和预期返工，再套用上面的 `pace` 策略，或按需改用成本目标 `weighted`、`main-model`；系数按记录在案的两次运行校准，是代理值，不是账单。

你可以增加未来模型或自定义 CLI 路由，只要它们声明能力和角色，并通过相同的身份、
权限、沙箱与证据检查。发布的示例与 schema 是 [`config/model-boss.example.json`](boss-dispatch/config/model-boss.example.json) 和 [`config/model-boss.schema.json`](boss-dispatch/config/model-boss.schema.json)；项目自动发现 `.model-boss.json`。POSIX 只在 `XDG_CONFIG_HOME` 是绝对路径时使用 `$XDG_CONFIG_HOME/model-boss/config.json`，否则使用 `$HOME/.config/model-boss/config.json`。PowerShell 中，绝对的 `$env:XDG_CONFIG_HOME` 优先；否则运行时先读取绝对的 `$env:HOME`，只在 HOME 缺失时回退到绝对的 `$env:USERPROFILE`。文档显示的 `$HOME\.config\model-boss\config.json` 使用 PowerShell 的 `$HOME` 便捷变量。被选中的根路径缺失或为相对路径时会安全失败。Profile 文件位于 [references/profiles](boss-dispatch/references/profiles)。

运行时 CLI 需要 Python 3.11+ 与 Git；POSIX 安装示例还会使用 `bash` 和 `install`。
可写的外部 Worker 还必须有验证过的 OS 后端：macOS 使用
`/usr/bin/sandbox-exec`，Linux（包括 WSL）使用 Bubblewrap（`bwrap`）。Windows
原生环境没有外部写 Worker 后端，只使用 Claude Code 或 Codex 的宿主原生 Agent。

## Claude Code 安装

以下命令会同时安装 Skill 与默认 Anthropic 角色资产；这些角色不会替换主循环。

**POSIX，用户级：**

```bash
git clone https://github.com/vincemakes/model-boss.git "$HOME/.local/share/model-boss"
bash "$HOME/.local/share/model-boss/boss-dispatch/install.sh"
mkdir -p "$HOME/.claude/agents"
for role in reviewer implementer mechanic scout; do
  install -m 0644 "$HOME/.claude/skills/boss-dispatch/assets/agents/claude-code/$role.md" \
    "$HOME/.claude/agents/model-boss-$role.md"
done
```

**POSIX，项目级：**

```bash
git clone https://github.com/vincemakes/model-boss.git .model-boss
mkdir -p .claude/skills
ln -sfn "$PWD/.model-boss/boss-dispatch" .claude/skills/boss-dispatch
mkdir -p .claude/agents
for role in reviewer implementer mechanic scout; do
  install -m 0644 ".claude/skills/boss-dispatch/assets/agents/claude-code/$role.md" \
    ".claude/agents/model-boss-$role.md"
done
```

**PowerShell，用户级：**

```powershell
$repo = Join-Path $HOME ".local\share\model-boss"
$skill = Join-Path $HOME ".claude\skills\boss-dispatch"
$agents = Join-Path $HOME ".claude\agents"
New-Item -ItemType Directory -Force (Split-Path $repo -Parent) | Out-Null
git clone https://github.com/vincemakes/model-boss.git $repo
New-Item -ItemType Directory -Force (Split-Path $skill -Parent) | Out-Null
if (Test-Path $skill) { Remove-Item -Recurse -Force $skill }
New-Item -ItemType SymbolicLink -Path $skill -Target (Join-Path $repo "boss-dispatch") | Out-Null
New-Item -ItemType Directory -Force $agents | Out-Null
foreach ($role in "reviewer", "implementer", "mechanic", "scout") {
  Copy-Item (Join-Path $skill "assets\agents\claude-code\$role.md") `
    (Join-Path $agents "model-boss-$role.md")
}
```

**PowerShell，项目级：**

```powershell
$repo = ".model-boss"
$skill = ".claude\skills\boss-dispatch"
$agents = ".claude\agents"
New-Item -ItemType Directory -Force (Split-Path $skill -Parent) | Out-Null
git clone https://github.com/vincemakes/model-boss.git $repo
if (Test-Path $skill) { Remove-Item -Recurse -Force $skill }
New-Item -ItemType SymbolicLink -Path $skill -Target (Join-Path $repo "boss-dispatch") | Out-Null
New-Item -ItemType Directory -Force $agents | Out-Null
foreach ($role in "reviewer", "implementer", "mechanic", "scout") {
  Copy-Item (Join-Path $skill "assets\agents\claude-code\$role.md") `
    (Join-Path $agents "model-boss-$role.md")
}
```

## Codex 安装

使用 Sol Profile 前先检查版本：

```bash
codex --version
```

`codex --version` 只用于诊断，不是能力证明。选择内置 Profile 前，Model Boss 的
preflight / 预检必须确认当前 Codex 支持自定义 Agent、当前账号与模型目录确实提供
所需的 Sol/Terra/Luna ID，并且目标沙箱与 reasoning 参数可用。可用性检查失败时返回
`provider_unavailable` 或 `reviewer_unavailable`；安装流程不会自动升级 Codex。

**POSIX，项目级：**

```bash
git clone https://github.com/vincemakes/model-boss.git .model-boss
mkdir -p .codex/skills
ln -sfn "$PWD/.model-boss/boss-dispatch" .codex/skills/boss-dispatch
mkdir -p .codex/agents
for role in reviewer implementer mechanic scout; do
  install -m 0644 ".codex/skills/boss-dispatch/assets/agents/codex/$role.toml" \
    ".codex/agents/model-boss-$role.toml"
done
```

**POSIX，用户级：**

```bash
git clone https://github.com/vincemakes/model-boss.git "$HOME/.local/share/model-boss"
bash "$HOME/.local/share/model-boss/boss-dispatch/install.sh"
mkdir -p "$HOME/.codex/agents"
for role in reviewer implementer mechanic scout; do
  install -m 0644 "$HOME/.codex/skills/boss-dispatch/assets/agents/codex/$role.toml" \
    "$HOME/.codex/agents/model-boss-$role.toml"
done
```

**PowerShell，项目级：**

```powershell
$repo = ".model-boss"
$skill = ".codex\skills\boss-dispatch"
$agents = ".codex\agents"
New-Item -ItemType Directory -Force (Split-Path $skill -Parent) | Out-Null
git clone https://github.com/vincemakes/model-boss.git $repo
if (Test-Path $skill) { Remove-Item -Recurse -Force $skill }
New-Item -ItemType SymbolicLink -Path $skill -Target (Join-Path $repo "boss-dispatch") | Out-Null
New-Item -ItemType Directory -Force $agents | Out-Null
foreach ($role in "reviewer", "implementer", "mechanic", "scout") {
  Copy-Item (Join-Path $skill "assets\agents\codex\$role.toml") `
    (Join-Path $agents "model-boss-$role.toml")
}
```

**PowerShell，用户级：**

```powershell
$repo = Join-Path $HOME ".local\share\model-boss"
$skill = Join-Path $HOME ".codex\skills\boss-dispatch"
$agents = Join-Path $HOME ".codex\agents"
New-Item -ItemType Directory -Force (Split-Path $repo -Parent) | Out-Null
git clone https://github.com/vincemakes/model-boss.git $repo
New-Item -ItemType Directory -Force (Split-Path $skill -Parent) | Out-Null
if (Test-Path $skill) { Remove-Item -Recurse -Force $skill }
New-Item -ItemType SymbolicLink -Path $skill -Target (Join-Path $repo "boss-dispatch") | Out-Null
New-Item -ItemType Directory -Force $agents | Out-Null
foreach ($role in "reviewer", "implementer", "mechanic", "scout") {
  Copy-Item (Join-Path $skill "assets\agents\codex\$role.toml") `
    (Join-Path $agents "model-boss-$role.toml")
}
```

原生 Agent TOML 只是默认配置，不是安全边界。Max Reviewer 必须由运行时确认实际子
进程的 fingerprint 与最终生效的只读权限。详情见
[Codex 适配器](boss-dispatch/references/adapters/codex.md)。

## Kimi 与 GLM 外部路由

Codex 可以调用已有 `claude-kimi*` / `claude-glm*` 命令；这不代表 Kimi 会原生
出现在 Codex 模型选择器中。Command name is not model identity / 命令名不是模型身份。

从安装后的项目目录把 wrapper 安装到明确指定的目录：

```bash
bash scripts/setup-model-providers.sh --install-path "$HOME/.local/bin"
```

只给出 `--install-path` 时，setup 只安装 wrappers；即使默认旧 credentials 文件存在，也不会检查或导入它。安装脚本不会修改 shell 启动文件；如果需要，请自行把该目录加入 `PATH`。Wrappers 本身不会让 Kimi 或 GLM 可用：还必须配置完整的直接环境或 credentials 文档，并单独安装可信 Provider 二进制。

直接环境中，Kimi 精确需要 `KIMI_BASE_URL` + `KIMI_AUTH_TOKEN`；GLM 精确需要 `GLM_BASE_URL` + `GLM_AUTH_TOKEN` + `GLM_MODEL` + `GLM_SMALL_FAST_MODEL`。

也可以创建严格的 version 1 JSON 文档，并在本地替换占位符：

```json
{
  "version": 1,
  "credentials": {
    "GLM_AUTH_TOKEN": "<glm-auth-token>",
    "GLM_BASE_URL": "<glm-base-url>",
    "GLM_MODEL": "<glm-model>",
    "GLM_SMALL_FAST_MODEL": "<glm-small-fast-model>",
    "KIMI_AUTH_TOKEN": "<kimi-auth-token>",
    "KIMI_BASE_URL": "<kimi-base-url>"
  }
}
```

POSIX 只在 `XDG_CONFIG_HOME` 是绝对路径时使用 `$XDG_CONFIG_HOME/model-boss/credentials.json`，否则使用 `$HOME/.config/model-boss/credentials.json`。选中的目录必须为 `0700`，文件必须为 `0600`；HOME 回退路径可执行：

```bash
chmod 0700 "$HOME/.config/model-boss"
chmod 0600 "$HOME/.config/model-boss/credentials.json"
```

PowerShell 中，绝对的 `$env:XDG_CONFIG_HOME` 优先；否则运行时先读取绝对的 `$env:HOME`，只在 HOME 缺失时回退到绝对的 `$env:USERPROFILE`。文档中的 `$HOME\.config\model-boss\credentials.json` 是常见 PowerShell 写法。绝对路径的 `MODEL_BOSS_CREDENTIALS` 可覆盖自动发现。绝不要把秘密放入仓库、`.model-boss.json` 或 `config/model-boss.example.json`。

精确的 wrapper 角色映射如下：

| 路由角色 | Reviewer transport 基础命令 | 只有验证过 OS 沙箱才允许的写命令 |
|---|---|---|
| Kimi Reviewer candidate | `claude-kimi` | — |
| Kimi implementer | — | `claude-kimi-bypass -p` |
| GLM Reviewer candidate | `claude-glm` | — |
| GLM implementer | — | `claude-glm-bypass -p` |
| GLM fast scout/mechanic | `claude-glm-turbo` | `claude-glm-turbo-bypass -p` |

先找到已安装的 `SKILL.md` 所在目录，并把它记为
`<model-boss-skill-root>`。目标仓库本身不需要包含 Model Boss。

密封 Max 工作流的顺序固定如下。计划与最终评审必须使用相同的有效评审身份/配置：
Reviewer route、实际 fingerprint、身份依据来源与只读证明；同时还必须使用相同的主循环
fingerprint。Profile 文件路径可以不同，但必须解析为这些完全相同的有效事实：

```bash
mkdir -p "$PWD/../model-boss-runs"
python3 <model-boss-skill-root>/scripts/model-boss.py plan-review \
  --repo "$PWD" \
  --temp-parent "$PWD/../model-boss-runs" \
  --task /absolute/path/to/task.json \
  --context /absolute/path/to/plan-context.json \
  --profile /absolute/path/to/profile.json \
  --route <reviewer-route> \
  --main-fingerprint <provider:model:variant>

python3 <model-boss-skill-root>/scripts/model-boss.py worker --manifest <manifest> \
  --repo "$PWD" \
  --temp-parent "$PWD/../model-boss-runs" \
  --route claude-kimi-bypass \
  --task /absolute/path/to/task.json \
  --mode max

python3 <model-boss-skill-root>/scripts/model-boss.py review \
  --profile /absolute/path/to/profile.json \
  --route <same-reviewer-route> \
  --main-fingerprint <same-provider:model:variant> \
  --manifest <manifest> \
  --context /absolute/path/to/review-context.json

python3 <model-boss-skill-root>/scripts/model-boss.py integrate <manifest>
```

Max 的 plan context 精确包含 `version`、`goal`、`proposed_plan`、
`acceptance_criteria` 和 `risks`。最终 context 必须重复已批准的 goal、plan 和验收
标准，只额外加入最终阶段专用的 `main_loop_verdict`。task、源码、计划或 Reviewer
任一发生变化都会阻止派发或审批。

Lite 在主循环内完成计划裁决。外部 Worker 自己创建 invocation，因此拒绝
`--manifest`：

```bash
python3 <model-boss-skill-root>/scripts/model-boss.py worker \
  --repo "$PWD" \
  --temp-parent "$PWD/../model-boss-runs" \
  --route claude-kimi-bypass \
  --task /absolute/path/to/task.json \
  --mode lite

python3 <model-boss-skill-root>/scripts/model-boss.py review --inline \
  --main-fingerprint <provider:model:variant> \
  --manifest <manifest> \
  --context /absolute/path/to/review-context.json

python3 <model-boss-skill-root>/scripts/model-boss.py integrate <manifest>
```

Worker 会创建一次性 worktree、重新探测沙箱、执行声明的 gates，并在不改动源仓库的
前提下密封 delta。最终评审通过后写入 invocation-bound receipt；集成只接受 manifest，
不接受调用者另传 approval 文件。

精确 task 与 review context schema 见
[外部 CLI 安全合同](boss-dispatch/references/adapters/external-cli.md)。不要在普通仓库里直接运行
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
Web 与 MCP 工具不可用。task 声明的 gate 命令使用直接参数数组，由 Model Boss 宿主
在模型调用后运行，不是授予模型的 shell 权限。

## 安全与失败行为

- Reviewer 只收到完整的 canonical evidence packet，不收到仓库、工具或凭据。
- 外部 Worker 只能写一次性 worktree 与本次调用的 state 目录。
- Prompt、日志、manifest 与 review packet 都不包含凭据值，但 Provider 客户端进程
  仍会获得连接 Endpoint 所需的凭据。应优先使用短期、窄权限 token，并限制到 route
  所需的最小权限。工具 allowlist 与文件系统沙箱不是网络安全边界：恶意或被入侵的
  Provider 二进制可以滥用它能读取的数据或收到的凭据，Model Boss 无法阻止它通过
  被允许的 Provider 网络连接发送这些内容。因此只应安装并运行可信的 Provider 二进制。
- 审批绑定三哈希；目标内容漂移即返回 `destination_changed` 并要求重新快照与审批。
- Model Boss 不 stash、reset、覆盖用户改动，也不靠 fuzzy apply 绕过冲突。
- 公开状态固定为 `ok`、`needs_context`、`gate_failed`、
  `provider_unavailable`、`reviewer_unavailable`、`timeout`、`scope_violation`、
  `transport_error`、`review_revise`、`approval_stale`、`destination_changed` 与
  `sandbox_unavailable`。

外部 CLI 细节见 [安全合同](boss-dispatch/references/adapters/external-cli.md)。

## 参考基准快照

现有数字来自历史 Claude/Fable/Opus 参考栈，不能预测 Sol、Kimi 或未来 Profile 的
节省比例。`-42%/-89%` 是当时记录的最强模型输出 token 变化，`-34%/-88%` 是报告
使用的价格加权额度代理。盲测 bug-hunt 只是一次观察，不是普遍性证明。

原始数字、方法与限制完整保留在
[BENCHMARKS.zh-CN.md](BENCHMARKS.zh-CN.md)。

Fable 5.1 effort 与派工复跑（从 [BENCHMARKS.zh-CN.md](BENCHMARKS.zh-CN.md) 链接过去，存于 `benchmarks/`）用 `benchmarks/harness/` 里的 harness 重跑了这个大型任务，是 `estimate` 系数的数据来源。

## Model Boss 何时让开

以下情况由主循环直接处理，不开启编排：单个包且小于约 200 行的改动；纯分析；尚未
定位根因的具体错误；安全或架构核心仍需要探索；用户只问模型价格/选择；可执行
规格比代码本身更长；用成本目标时还包括 `estimate` 算出来交接省不到 10%。

## 从 Token Saver 迁移

迁移是显式且 no-overwrite 的。正常自动发现会忽略所有旧路径与旧环境变量。`--legacy-source` 必须显式提供才会导入；默认旧文件不会导入（wrapper-only setup 只安装 wrappers）。唯一标准的旧 Provider 导入命令是：

```bash
python3 <model-boss-skill-root>/scripts/model-boss.py setup-providers --legacy-source <absolute-old-providers.env>
```

该命令只会把指定的旧 `$HOME/.claude/fable-token-saver/providers.env` 格式文件当作数据解析，绝不会当作 shell 代码。`scripts/setup-model-providers.sh` 只是这条标准命令的 wrapper。迁移绝不删除或编辑旧数据。

旧 JSON credentials 绝不自动复制。手动复制前必须先检查文件和目录权限，或者让 `MODEL_BOSS_CREDENTIALS` 指向现有 JSON 的绝对路径。旧环境变量会被忽略；下列是手动迁移映射，不是兼容别名。

| 旧表面 | Model Boss 表面 |
|---|---|
| `https://github.com/vincemakes/token-saver` | `https://github.com/vincemakes/model-boss` |
| `.claude/skills/token-saver`, `.agents/skills/token-saver` | `.claude/skills/boss-dispatch`, `.agents/skills/boss-dispatch` |
| `scripts/token-saver-route.py` | `scripts/model-boss.py` |
| `runtime.token_saver` | `runtime.model_boss` |
| `.token-saver.json` | `.model-boss.json` |
| `XDG_CONFIG_HOME` 为绝对路径时的 `$XDG_CONFIG_HOME/token-saver/config.json` | `$XDG_CONFIG_HOME/model-boss/config.json` |
| 否则的 `$HOME/.config/token-saver/config.json` | `$HOME/.config/model-boss/config.json` |
| PowerShell 在 `XDG_CONFIG_HOME` 非绝对路径时的 `$HOME\.config\token-saver\config.json` | `$HOME\.config\model-boss\config.json` |
| `XDG_CONFIG_HOME` 为绝对路径时的 `$XDG_CONFIG_HOME/token-saver/credentials.json` | `$XDG_CONFIG_HOME/model-boss/credentials.json` |
| 否则的 `$HOME/.config/token-saver/credentials.json` | `$HOME/.config/model-boss/credentials.json` |
| PowerShell 在 `XDG_CONFIG_HOME` 非绝对路径时的 `$HOME\.config\token-saver\credentials.json` | `$HOME\.config\model-boss\credentials.json` |
| `TOKEN_SAVER_CREDENTIALS` | `MODEL_BOSS_CREDENTIALS` |
| `TOKEN_SAVER_INVOCATION_MANIFEST` | `MODEL_BOSS_INVOCATION_MANIFEST` |
| `TOKEN_SAVER_TRUSTED_GATE_FAILURES` | `MODEL_BOSS_TRUSTED_GATE_FAILURES` |
| `TOKEN_SAVER_PROVIDER_API_KEY` | `MODEL_BOSS_PROVIDER_API_KEY` |
| `token-saver-<role>.md`, `token-saver-<role>.toml` | `model-boss-<role>.md`, `model-boss-<role>.toml` |
| `token-saver-runs` | `model-boss-runs` |
| `config/token-saver.example.json`, `config/token-saver.schema.json` | `config/model-boss.example.json`, `config/model-boss.schema.json` |
| `dist/token-saver.skill` | `dist/model-boss.skill` |

## 许可证

[MIT](LICENSE)
