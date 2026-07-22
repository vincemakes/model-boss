# Model Boss 开发笔记

Model Boss 是 Claude Code 与 Codex 共用的跨模型编程编排工作流。宿主已选定的主循环是不可替换的输入；Lite 在主循环内 inline 持有权威，Max 由独立且已验证的 Reviewer 持有权威。当前仓库是 <https://github.com/vincemakes/model-boss>。

## 当前开发地图

- 工作流协议：[`SKILL.md`](../SKILL.md)、[`references/protocol.md`](../references/protocol.md)、[`references/routing.md`](../references/routing.md)。
- 运行时：`runtime/model_boss/`；CLI 入口：`python3 scripts/model-boss.py`。
- 配置：[`config/model-boss.example.json`](../config/model-boss.example.json) 与 [`config/model-boss.schema.json`](../config/model-boss.schema.json)；项目级文件为 `.model-boss.json`。
- 用户级自动发现：`${XDG_CONFIG_HOME:-$HOME/.config}/model-boss/config.json` 与 `${XDG_CONFIG_HOME:-$HOME/.config}/model-boss/credentials.json`。
- 打包命令：`bash scripts/package-skill.sh`；标准产物：`dist/model-boss.skill`。
- 验证命令：`bash scripts/validate.sh` 与 `python3 -m unittest discover -s tests -v`。

## 评测与安全维护

历史数字仅是 2026 年 Claude/Fable/Opus 参考栈的单次观察，不能推导 Sol、Kimi 或未来 Profile。完整方法、原始数字、负面结果与限制见 [`BENCHMARKS.zh-CN.md`](../BENCHMARKS.zh-CN.md)。复现时每组条件应使用独立进程和全新 fixture，保留 CLI JSON 中的 `modelUsage`，并将盲测集隔离到运行结束后再注入。

维护时不得改变密封证据、三哈希审批绑定、一次性 worktree、经验证的 OS 沙箱、Max 评审身份分离以及 fail-closed 状态语义。外部 Provider 二进制仍是信任边界：工具 allowlist 和文件系统沙箱不是网络安全边界。

## 从 Token Saver 迁移

本项目在 2026-07 更名。历史 `fable-token-saver` 工作区与 v0.1.0 评测数据记录了小任务负收益、大型 Lite/Max 构建、单次盲测 bug-hunt，以及主循环身份自判不可靠等早期经验。这些只是历史与复现上下文，不是当前操作说明。

| 旧表面 | Model Boss 表面 |
|---|---|
| `https://github.com/vincemakes/token-saver` | `https://github.com/vincemakes/model-boss` |
| `.claude/skills/token-saver`, `.agents/skills/token-saver` | `.claude/skills/model-boss`, `.agents/skills/model-boss` |
| `scripts/token-saver-route.py` | `scripts/model-boss.py` |
| `runtime.token_saver` | `runtime.model_boss` |
| `.token-saver.json` | `.model-boss.json` |
| `${XDG_CONFIG_HOME:-$HOME/.config}/token-saver/config.json` | `${XDG_CONFIG_HOME:-$HOME/.config}/model-boss/config.json` |
| `$HOME/.claude/fable-token-saver/providers.env`, `${XDG_CONFIG_HOME:-$HOME/.config}/token-saver/credentials.json` | `${XDG_CONFIG_HOME:-$HOME/.config}/model-boss/credentials.json` |
| `TOKEN_SAVER_*` | `MODEL_BOSS_*` |
| `token-saver-<role>.md`, `token-saver-<role>.toml` | `model-boss-<role>.md`, `model-boss-<role>.toml` |
| `token-saver-runs` | `model-boss-runs` |
| `config/token-saver.example.json`, `config/token-saver.schema.json` | `config/model-boss.example.json`, `config/model-boss.schema.json` |
| `dist/token-saver.skill` | `dist/model-boss.skill` |

正常自动发现会忽略上述旧路径与 `TOKEN_SAVER_*`。只有 `scripts/setup-model-providers.sh` 可以显式读取 `$HOME/.claude/fable-token-saver/providers.env`，并只把它当作数据解析，不会 source。迁移只做 no-overwrite 复制，绝不删除或编辑旧数据。


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
