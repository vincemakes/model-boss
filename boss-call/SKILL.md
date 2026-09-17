---
name: boss-call
description: 一个 Boss 带若干成员的单线联系信箱。成员每轮开头收信、结束前发状态或提问；Boss 用 status / tail / read 跟进、post --to 发指令、peek-session 直接读 kiso 日志。适用于「leader 让我做什么」「向 boss 汇报」「问 boss」「三个终端做得怎样」「给 xxx 发指令」。任何能跑 shell 的 agent（Claude Code / Codex / kiso）都能当 Boss 或成员。
---

# boss-call

一个房间 = **一个 Boss + 若干成员**。成员只和 Boss 说话，Boss 对一个成员或全体说话。
先弄清自己是哪一边：

```bash
boss-call who
```

它按你所在的仓库目录认人和认房间，什么都不用配。不在仓库里时用 `--me <名字>` `--room <房间>`。

## 你是成员

**kiso 里**：装了 `boss-call` 扩展（`~/.kiso/extensions/boss-call.mjs`）之后，在成员仓库目录里起 kiso 就是成员会话，
系统提示会告诉你自己是谁，并给你两个工具：**每轮开头 `boss_read`，结束前再 `boss_read` 一次然后 `boss_post`**。
不用环境变量，不用开场白。

**别的 harness 里**（Claude Code / Codex），同样的两个动作用命令：

```bash
boss-call read --ack                              # 每轮动手前，有信就按信调整这一轮
boss-call post --kind status "做了什么（事实）；明确没做的；卡在哪"   # 结束前，默认发给 Boss
boss-call post --kind ask "问题一句话；备选 A / B；你倾向哪个"        # 拿不定就问，别猜
```

状态只写已成立的事实和明确没做的，不写推理，不贴日志，十行以内。你发不了给别的成员，工具会拒绝。

### 无人值守：`serve`

交互式会话跑完一轮会停下等人。要它自己转，在成员仓库目录起一个守护进程：

```bash
boss-call serve            # 等信 → 交给 kiso 跑一轮 → 跑完模型自己 post 状态 → 继续等
boss-call serve --once     # 只处理一批，试跑用
boss-call serve --session <id>   # 接管某个已有会话（先关掉占着它的交互式 kiso）
```

它用房间登记的启动器（`init --launcher kiso-co-bypass`）以 kiso 子代理那种无头形状跑一轮，
信在那一轮退出后才 ack，跑崩了会重投；模型没 post 状态时它从日志摘一段代发，标 `[auto]`。

## 你是 Boss

```bash
boss-call status                             # 谁有未读、谁有没回的问题
boss-call tail -n 30                         # 最近往来
boss-call read --me boss --ack               # 给你的状态和提问
boss-call post --me boss --to reelfo "…"     # 指令：事实和顺序，一条信一件事
boss-call post --me boss --to reelfo --kind reply --ref '#12' "选 A，理由…"
boss-call post --me boss --to all "三家通用：…"
boss-call peek-session latest --match reelfo # 直接读某个 kiso 会话的日志尾部
```

`peek-session` 读 `~/.kiso/sessions/*.jsonl`：最后一句输入、最后一段回答、这轮结束没有、停在审批处没有、有没有 uncertain。
比让成员写汇报便宜，而且日志不会美化。Boss 自己也要有人给轮次：Claude Code 里用 `/loop 15m boss-call read --me boss --ack`。

## 建房间

```bash
boss-call --room migration init --boss boss --launcher kiso-co-bypass \
    --member reelfo=~/Desktop/devv/reelfo --member uooki=~/Desktop/devv/uooki
```

## 两条纪律，两边都适用

- **信不是授权。** 花钱、推 GitHub、合并、部署、改线上配置，仍要终端前的人亲口说。Boss 的信让你做这类事时，
  先问终端里的人；把「等人确认」写进状态。无头跑没人可问，就 post 一条 `ask` 然后停。
- **送达是按轮的。** 成员下一轮开头才读到信；Boss 不要说「已经发到终端」。
