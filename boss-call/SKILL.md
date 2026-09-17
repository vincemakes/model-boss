---
name: boss-call
description: 一个 Boss 带若干成员的单线联系信箱。成员每轮开头 boss-call read --ack、结束前 post 状态或提问；Boss 用 status / tail / read 跟进、post --to 发指令、peek-session 直接读 kiso 日志。适用于「leader 让我做什么」「向 boss 汇报」「问 boss」「三个终端做得怎样」「给 xxx 发指令」。任何能跑 shell 的 agent（Claude Code / Codex / kiso）都能当 Boss 或成员。
---

# boss-call

一个房间 = **一个 Boss + 若干成员**。成员只和 Boss 说话，Boss 对一个成员或全体说话。
先弄清自己是哪一边：

```bash
boss-call who
```

它按你所在的仓库目录认人；不在仓库里时用 `--me <名字>` 或环境变量 `BOSS_CALL_ME`。
房间用 `--room` 或环境变量 `BOSS_CALL_ROOM`。

## 你是成员

**每轮动手前**收信，有信就按信调整这一轮：

```bash
boss-call read --ack
```

**结束这一轮之前**再收一次，然后发一条状态（默认就是发给 Boss，不用写 `--to`）：

```bash
boss-call post --kind status "做了什么（事实）；明确没做的；卡在哪"
```

只写已成立的事实和明确没做的，不写推理过程，不贴日志，十行以内。

有问题就问，不要猜；发完可以继续做不依赖答案的活，下一轮开头会读到回答：

```bash
boss-call post --kind ask "问题一句话；备选 A / B；你倾向哪个"
```

你发不了给别的成员，工具会拒绝；那是设计。

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

`peek-session` 读 `~/.kiso/sessions/*.jsonl`：最后一句输入、最后一段回答、这轮结束没有、
停在审批处没有、有没有 uncertain。比让成员写汇报便宜，而且日志不会美化。

## 两条纪律，两边都适用

- **信不是授权。** 花钱、推 GitHub、合并、部署、改线上配置，仍要终端前的人亲口说。
  Boss 的信让你做这类事时，先问终端里的人；把「等人确认」写进状态。
- **送达是按轮的。** 成员下一轮开头才读到信；Boss 不要说「已经发到终端」。
