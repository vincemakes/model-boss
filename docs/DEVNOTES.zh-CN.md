# Model Boss 开发笔记

Model Boss 是 Claude Code 与 Codex 共用的跨模型编程编排工作流。宿主已选定的主循环是不可替换的输入；Lite 在主循环内 inline 持有权威，Max 由独立且已验证的 Reviewer 持有权威。当前仓库是 <https://github.com/vincemakes/model-boss>。

## 当前开发地图

- 工作流协议：[`SKILL.md`](../boss-dispatch/SKILL.md)、[`references/protocol.md`](../boss-dispatch/references/protocol.md)、[`references/routing.md`](../boss-dispatch/references/routing.md)。
- 运行时：`runtime/model_boss/`；CLI 入口：`python3 scripts/model-boss.py`。
- 配置：[`config/model-boss.example.json`](../boss-dispatch/config/model-boss.example.json) 与 [`config/model-boss.schema.json`](../boss-dispatch/config/model-boss.schema.json)；项目级文件为 `.model-boss.json`。
- 用户级自动发现：POSIX 只在 `XDG_CONFIG_HOME` 是绝对路径时使用 `$XDG_CONFIG_HOME/model-boss/config.json` 与 `$XDG_CONFIG_HOME/model-boss/credentials.json`，否则使用 `$HOME/.config/model-boss/config.json` 与 `$HOME/.config/model-boss/credentials.json`。PowerShell 中，绝对的 `$env:XDG_CONFIG_HOME` 优先；否则运行时先读取绝对的 `$env:HOME`，只在 HOME 缺失时回退到绝对的 `$env:USERPROFILE`。文档中的 `$HOME\.config\model-boss\config.json` 与 `$HOME\.config\model-boss\credentials.json` 使用 PowerShell `$HOME` 便捷变量；被选中的根路径缺失或为相对路径时安全失败。
- 打包命令：`bash scripts/package-skill.sh`；标准产物：`dist/boss-dispatch.skill`。
- 验证命令：`bash scripts/validate.sh` 与 `python3 -m unittest discover -s tests -v`。

## 2026-09 路由升级：精确版本、effort、算账

- 路由字段新增 `effort`（low/medium/high/xhigh/max）、`quota_weight`（默认 1.0）、`aliases`。`effort` 不进身份指纹：同一模型不同 effort 在权威分离上仍是同一个模型，`runtime/model_boss/catalog.py` 按模型目录校验档位（Opus 4.6 / Sonnet 4.6 没有 xhigh，Haiku 4.5 不接受 effort）。
- 默认 Anthropic Profile 覆盖 Claude Code 选择器 2026-09-02 暴露的 9 个模型，每个高级模型拆成只读 Reviewer 路由和可写 Worker 路由（旧 Profile 里 `opus` 只有 reviewer 角色，README 示例「让 opus 去开发」实际解析不到 Worker）。默认 Worker 偏好是 `opus-5-worker` 再 `sonnet-5`；路由选择优先与主循环模型不同的 Worker。
- 新命令 `match-models` 把用户口述的模型名按最长匹配映射到路由，目录外版本返回 `needs_context`；`estimate` 按任务形状给 inline / Lite / Max 算账（`runtime/model_boss/dispatch.py`），系数锚定 BENCHMARKS 那一次 1,100 行的记录，只是代理值。
- 已核实的缓存事实（Claude Code 与 API 文档）：缓存按模型、按 effort 隔离，切换即整段重算；子代理不读父会话缓存，订阅下主会话 1 小时 TTL、子代理 5 分钟（`subagentPromptCacheTtl`）；Fable 5.1 缓存读价 $0.25/MTok（其他模型 0.1 倍基价）；跨模型带得走的是思考块（只在切到 Fable 5.1 的方向），不是缓存。
- 未核实：按模型的订阅额度倍率没有公开文档，所以做成 `quota_weight` 可配置，默认 1.0。
- 真实 benchmark 复跑已完成，结果在 [`benchmarks/fable-effort-dispatch-rerun.md`](../benchmarks/fable-effort-dispatch-rerun.md)（原始数据 `.json` 同名）。harness 在 `benchmarks/harness/`（`run_cells.py`，6 个格子，Fable 只跑 low/medium），以独立 `claude -p` 子进程运行，要求终端里 `claude auth login` 过；从桌面 App 宿主会话派生的子进程借不到宿主鉴权。Fable 5.1 要求 CLI 2.1.251+，harness 有版本预检；2026-09-02 为此把 nvm 里的 CLI 从 2.1.246 升到 2.1.258。
- 策略层（用户确认额度语义后定稿）：Claude Max 是一个共用总池加 Fable 一半上限；用户是质量优先而不是成本敏感。`estimate` 默认目标改为 `pace`（节奏内选最强）：执行体量大、规格清楚的任务走 Fable 主循环 + Opus worker（xhigh，独立包并行），四种例外是判断密集、单包小于约 200 行、单流交互、Fable 半区超前节奏（后者建议下个会话用 Opus 主循环 + Fable reviewer 的 Max）。节奏输入是 /usage 的三个百分比，目标 Fable 占总花费 50%。成本目标 `weighted`、`main-model` 保留给按量付费用户。大于 1,000 行的 Fable 节省数字是模型外推，未实测。
- 复跑结论：Fable 5.1 low 单干最便宜最快（$1.31，比 Fable 5 high 基线省 57%）；Opus 5 worker 几乎不省 Fable 且总额翻倍；Sonnet 5 worker 省约三分之一 Fable 但总额更高；Lite 里 Fable 主循环六到七成花费是读 worker 报告和 diff 的缓存写入。`dispatch.py` 据此重校准，并加了 worker 产出体积因子和子代理 5 分钟 TTL 计价。

## 评测与安全维护

历史数字仅是 2026 年 Claude/Fable/Opus 参考栈的单次观察，不能推导 Sol、Kimi 或未来 Profile。完整方法、原始数字、负面结果与限制见 [`BENCHMARKS.zh-CN.md`](../BENCHMARKS.zh-CN.md)。复现时每组条件应使用独立进程和全新 fixture，保留 CLI JSON 中的 `modelUsage`，并将盲测集隔离到运行结束后再注入。

维护时不得改变密封证据、三哈希审批绑定、一次性 worktree、经验证的 OS 沙箱、Max 评审身份分离以及 fail-closed 状态语义。外部 Provider 二进制仍是信任边界：工具 allowlist 和文件系统沙箱不是网络安全边界。

## Provider 凭据与 wrapper setup

`bash scripts/setup-model-providers.sh --install-path "$HOME/.local/bin"` 只安装 wrappers。即使默认旧 credentials 文件存在，也不会检查或导入它。Wrappers 本身不会让 Kimi 或 GLM 可用；还必须安装可信 Provider 二进制，并提供完整凭据。

直接环境中，Kimi 精确需要 `KIMI_BASE_URL` + `KIMI_AUTH_TOKEN`；GLM 精确需要 `GLM_BASE_URL` + `GLM_AUTH_TOKEN` + `GLM_MODEL` + `GLM_SMALL_FAST_MODEL`。严格的 version 1 JSON 形状是：

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

POSIX 上选中的 credentials 目录必须是 `0700`，文件必须是 `0600`；HOME 回退可使用 `chmod 0700 "$HOME/.config/model-boss"` 和 `chmod 0600 "$HOME/.config/model-boss/credentials.json"`。绝不要把秘密放入仓库、`.model-boss.json` 或 `config/model-boss.example.json`。

## 从 Token Saver 迁移

本项目在 2026-07 更名。历史 `fable-token-saver` 工作区与 v0.1.0 评测数据记录了小任务负收益、大型 Lite/Max 构建、单次盲测 bug-hunt，以及主循环身份自判不可靠等早期经验。这些只是历史与复现上下文，不是当前操作说明。

迁移是显式且 no-overwrite 的。正常自动发现会忽略所有旧路径与旧环境变量。`--legacy-source` 必须显式提供才会导入；默认旧文件不会导入（wrapper-only setup 只安装 wrappers）。唯一标准的旧 Provider 导入命令是：

```bash
python3 scripts/model-boss.py setup-providers --legacy-source <absolute-old-providers.env>
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

### 历史评测地图与复现方法

- 原始工作区为 `~/Desktop/devv/fable-token-saver`，v0.1.0 本地安装为 `~/.claude/skills/fable-token-saver`，非 Git 评测数据位于 `~/.claude/skills/fable-token-saver-workspace/`。这些路径只用于复现旧运行。
- 每组条件使用独立 headless 进程：`claude -p "<prompt>" --model <id> --dangerously-skip-permissions --output-format json`；原始数字来自 JSON 的 `modelUsage`（in/out/cacheRead/cacheWrite/costUSD）。
- fixture 是已初始化 Git、带 tsc + vitest 闸门的小型 pnpm 项目；每次运行都使用全新拷贝。对照组 prompt 相同，仅在是否先调用 `fable-token-saver` skill 上有一句差异。
- 盲测 bug-hunt 在运行结束后才注入 `benchmarks/bughunt/hidden.test.ts`，覆盖半开区间双计费、逐行舍入漂移、内部引用泄漏、异步竞态、缓存不失效和数字键字典序六类问题。
- “额度代理”指该模型名下的 `costUSD`，是对输入、输出与 cache 的价格加权，不是未公开的官方额度公式。

### 历史观察（单次运行）

- 小任务（少于 300 行）的编排开销为负收益，因此需要委托门槛。
- 当时的大型构建记录中，Lite 额度代理为 −34%；Max 为 −88%，但总费用增加 86%。这些数字不能外推。
- 盲测调试两组都是 6/6，但编排组的费用与时间更高，支持“未定位根因的调试不触发编排”这一边界。
- 当时观察到高级主循环的两个干净检查点很重要；频繁保留顾问会让成本与协调开销同时上升。

### 历史踩坑记录

1. 在 Claude Code 会话内启动 `claude -p` 子进程时要使用最小环境；继承 OAuth/SDK 变量曾导致子 CLI 直接 401。
2. 早期 skill-creator 触发检测与 CLI 流事件不兼容，所以真实触发需要活探针，不能只信评测 harness。
3. `SKILL.md` description 受 1024 字符限制；fixture 的 `node_modules` 不得进入 Git，否则会污染 diff-only 审查。
4. zsh 的 `echo ===` 与 macOS BSD sed 的语法差异曾导致复现脚本失败；脱敏管道应使用可移植实现。
5. 背景单包派工的结果通知路由曾经不可靠，因此早期工作流改为前台阻塞派工。
6. Agent 从静态身份串自判主循环模型曾造成静默误分类。这一历史经验最终演化为当前的宿主结构化身份、canonical fingerprint 与 fail-closed 约束。

未完成的历史方向包括：验证超过 5,000 行任务的总费用交叉点，对关键数字运行 3–5 次以估计方差，以及将同一盲测 fixture 复用到其他模型组合。
