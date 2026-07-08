# fable-token-saver

![fable-token-saver — Claude Code 分层模型编排](media/og.png)

[English](README.md) | **简体中文**

**Claude Code 的分层模型编排。** 让最贵的模型(Fable、Opus——阵容里最强的那个)只干判断力工作——拆解、写规格、审查;实现全部交给便宜档(Sonnet/Haiku)。客观闸门(类型检查/测试)先把机器能抓的错全部过滤掉,贵模型一个 token 都不花在这些上面。

## 该不该用?(一屏看完整个项目)

**✅ 有意义:大型 + 构建型 + 可写规格的任务**(300+ 行 / 6+ 文件——重构、迁移、从零建子系统):

- **lite 档**(Fable 主循环):额度 **−34%**,总费用*还是*最低的,质量甚至略好(测试多 12%)——**日常默认,无脑开**
- **max 档**(Opus 主循环 + Fable 顾问):额度 **−88%**,能力持平已用盲测调试赛实证(6/6 vs 6/6)——但总费用 **+86%**,是拿美元换额度。**只在 Fable 额度见底、又必须继续干活时切**

**❌ 没意义(skill 会自己检测并让位——这也是实测的):**

- **小任务**(< ~300 行):额度反升 34~66%
- **调试 / 判断密集型任务,不管多大**:推理没法下放——max 档花 2.2 倍钱、慢 3.9 倍,换来完全相同的结果

以上每个数字都来自真实运行——完整表格和方法论见 [BENCHMARKS.zh-CN.md](BENCHMARKS.zh-CN.md)。

## 工作原理

每个 Claude Code 会话都有一个**主循环**——你用 `/model` 选的那个模型。主循环要为它经手的一切付钱:每个工具返回、每次读文件、每轮簿记。这笔"主循环税"占了会话成本的大头,而且 skill 无法更换主循环模型——只有你能,通过 `/model`。skill *能*做的是决定每个**子代理**用什么模型(Agent 工具支持按次指定 `model` 参数,用版本无关的别名)。

这个 skill 同时利用这两个事实:

```
 lite 档  (/model = Fable)                max 档  (/model = Opus 4.8)
 ┌───────────────────────────┐            ┌───────────────────────────┐
 │ FABLE · 主循环            │            │ OPUS · 主循环             │
 │ 分类 → 写规格 → 审查      │            │ 分类 → 写规格 → 审查      │
 └─────┬─────────────────────┘            └─────┬───────────────┬─────┘
       │ 任务包                                 │ 任务包        │ 仅2次简报
       ▼                                        ▼               ▼
 ┌──────────┐  ┌──────────┐              ┌──────────┐   ┌──────────────┐
 │ SONNET   │  │ HAIKU    │              │ SONNET   │   │ FABLE        │
 │ 写代码   │  │ 机械活   │              │ 写代码   │   │ 顾问:       │
 └────┬─────┘  └────┬─────┘              └────┬─────┘   │ 计划裁决     │
      │  闸门: tsc+测试 全绿 ──┐              │  闸门   │ 合并前终审   │
      ▼                        ▼              ▼         └──────────────┘
     主循环只审 diff                        审 diff → 顾问放行
```

- **任务包**,绝不转发对话历史:worker 只拿 GOAL / CONTEXT(≤5行)/ ALLOWED FILES / SPEC / GATE / DO-NOT 围栏。
- **闸门先于审查**:worker 在*自己的*上下文里跑 `tsc && 测试`,自修到绿(最多 3 次)。没过闸的代码永远不会被审。
- **只审 diff**,审设计和意图——语法、类型、测试真伪归闸门管。
- **max 档里,最强模型是无状态顾问**:中档主循环*起草*计划(长文归便宜档),Fable 只*裁决*,最后对 diff 放行或打回。两次"简报进、裁决出",零会话上下文(实测:缓存读 0 token)。

## 两档模式——选主循环模型就是选档

不需要配置文件:skill 探测主循环模型后自动适配。档位名描述的是**省 token 的力度**(lite 省得少、max 省得满)——不是模型强度,类似汽车的 eco 模式。

| | **lite 档** | **max 档** |
|---|---|---|
| 主循环(`/model` 切换) | 最强档(Fable) | 中档(**推荐 Opus**) |
| 代码谁写 | Sonnet / Haiku worker | Sonnet / Haiku worker |
| 最强档干什么 | 全程掌舵每一步 | 只在 2 个检查点当顾问 |
| 最强档额度 | **−34%(实测)** | **−88%(实测,Opus 主循环)** |
| 总费用(美元) | 编排方案里最低 | **比基线贵 +86%**——拿美元换额度 |
| 什么时候用 | 想要顶级判断力盯每一步 | 最强档额度就是你的硬约束 |

## Benchmarks(电梯版)

同一个 1,100 行的从零构建任务,四种配置,闸门全绿、质量断言完全一致:

- **lite 档**:最强档额度 **−34%**,编排方案里总费用最低,产出测试最多
- **max 档(Opus 主循环)**:最强档额度 **−88%**,最快(116s vs 基线 335s),但总费用 +86%
- **能力持平已用盲测调试赛验证**:6 颗生产级埋雷、只给症状报告、盲测试集判分——基线 Fable **6/6**,max 档 **6/6**,根因推理能力在顾问架构下完整保留
- **小任务(< ~300 行)和调试任务:负收益**(小任务最强档 +34~66%;调试赛里 max 档花 2.2 倍价钱换来相同质量)——skill 都会自己检测并让位

完整的四组对照表、逐项额度账单、各模型价目、测试方法和诚实结论(包括为什么 Sonnet 当主循环两头输):**[BENCHMARKS.zh-CN.md](BENCHMARKS.zh-CN.md)**。

## 安装

```bash
git clone https://github.com/<you>/fable-token-saver ~/.claude/skills/fable-token-saver
```

Agent 工具支持按子代理覆写模型的环境(当前 Claude Code 支持)装完即用。可选:安装固定路由的 worker agent 定义:

```bash
mkdir -p .claude/agents
cp ~/.claude/skills/fable-token-saver/assets/agents/*.md .claude/agents/
```

### 可选:外部模型 worker(GLM / Kimi)

干活的模型不一定来自你的 Anthropic 账号。一条命令安装 CLI 包装器,让 Claude Code 跑在 GLM(智谱)或 Kimi 的 Anthropic 兼容端点上:

```bash
bash scripts/setup-model-providers.sh                                  # 交互式:提示输入 key,或自动迁移
KIMI_AUTH_TOKEN=sk-... GLM_AUTH_TOKEN=... bash scripts/setup-model-providers.sh   # 非交互式
```

API key 存到 `~/.claude/fable-token-saver/providers.env`(chmod 600,永不进 repo 或 shell rc),`~/.zshrc` 里旧式的 `claude-kimi()`/`claude-glm()` 函数会被自动迁移;安装 `claude-kimi`、`claude-glm`、`claude-glm-turbo` 及各自的 `-bypass` 变体(`--dangerously-skip-permissions`,headless 派发必需)。脚本幂等,随时可重跑——更新包装器或换 key(新 key 用环境变量传入)都直接重跑即可;只给一个 provider 的 key 就只装那一个。不带后缀的命令给你自己交互用;编排者则用 headless 方式派发任务包。任何主循环模型都能派——lite 档的 Fable、max 档的 Opus/Sonnet 同理:"用kimi开发,你审核",Kimi 写代码,主循环模型审 diff:

```bash
claude-kimi-bypass -p "<任务包>"
```

## 使用

三种启动方式,任何档位通用:

1. **显式调用** —— `/fable-token-saver 把这个模块重构了`
2. **自然语言** —— "用 token saver 走一下"、"省token模式做这个"、"把这个委托给便宜模型做,你负责审查"
3. **主动触发** —— 大任务(约 300+ 行或 6+ 文件)时 skill 自己触发;低于委托门槛时它会刻意不掺和。

## 什么时候别用它

一行改动、纯分析讨论、设计密集型核心、以及低于委托门槛的一切任务。benchmark 说明了原因:~300 行以下,"写任务包 + 审查"比直接把改动敲出来还贵。skill 自己会说"这个我不该接"然后让位。

## License

MIT
