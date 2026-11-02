# IM 信道、Trigger 入口与卡片确认（W5–W6）

对应方案 2.4.2（Trigger）、2.7（L2 单步获准）、5.2（按需界面）、第 6 章（IM 接入）。

## 一条消息的路径

```
钉钉 Stream 长连接 ──► parse_bot_message / parse_card_callback ──► InboundMessage（统一格式）
                                                                      │
                                                         TriggerIntake.handle()
   ┌──────────────────────────────────────────────────────────────────┤
   │ 1 群聊没 @机器人 → 丢弃，不落库                                       │
   │ 2 同一平台消息 id 只处理一次（triggers 表唯一约束）                       │
   │ 3 归入 Thread（一个群 / 一个单聊一个 Thread；卡片点击归入卡片的 Thread）     │
   │ 4 IM 账号 → 系统用户（channel_identities）；没绑定只回一句提示              │
   │ 5 Fast Path 分流：                                                  │
   │     卡片按钮 / 文字指令「确认 K7Q2M」 → 卡片确认（L2）                    │
   │     图片、文件 → 文档解析（与上传接口同一入口 ingest_document）             │
   │     其他文字 → route=router，status=pending（Router 在 W7–W8 接手）      │
   └──────────────────────────────────────────────────────────────────┘
                                                                      │
                                  事务提交之后 deliver() 发回复（避免“说了已记账但回滚了”）
```

每条进入系统的消息在 `triggers` 表留一条记录（来源、Thread、发送人、分流结果、处理结果）。
这是运行记录，**不是业务事实**；业务事实仍只通过 `Ledger.append()` 写入。

## 卡片确认（L2）

Agent 或业务代码用 `propose_card()`（或 `POST /v1/cards`）提出一张卡片，附带“确认后要执行的动作”。
目前只有一种动作：向事件账本追加一个事件（`ledger.append`）。

- 动作以**点击人自己的身份和权限**执行。提出卡片的一方（以后是 Agent）不需要、也不获得写权限。
- 点击人权限不够 → 不写入，卡片保持可确认，留给有权限的人。
- 一张卡片只生效一次：先把状态从 `open` 抢占为 `acting`（条件更新，PostgreSQL 下并发点击会串行），
  事件再带幂等键 `card:<卡片 id>`，双重保证不会重复记账。
- 事件自动附上审批证据 `{"kind": "approval", "ref": "card:<id>"}`，`correlation_id` 为卡片所在 Thread。
- 可以限定确认人（`allowed_login`）或确认所需权限（`required_permission`）；默认 72 小时过期。
- 卡片的 `data_mode` 必须与要写入的事件一致，模拟卡片上会显示“模拟数据，非真实识别结果”。

平台没有按钮回调时（钉钉未配置互动卡片模板），卡片退回 Markdown，并提示回复文字指令，如「@我 确认 K7Q2M」。
短码 5 位，去掉了易混的 I、L、O、0、1，大小写都认，只在本租户内有效。

## 钉钉接入步骤（首次接真实应用时）

1. 钉钉开放平台创建**企业内部应用**，添加**机器人**能力，消息接收模式选 **Stream 模式**。
2. 权限：机器人发消息（群聊、单聊）、下载机器人接收的文件；用互动卡片时加卡片相关权限。
3. 在部署环境（`deploy/.env` 或 KMS）填写，**不要写进代码或租户配置**：
   ```
   FOS_DINGTALK_LUNENG_CLIENT_ID=      # AppKey / Client ID
   FOS_DINGTALK_LUNENG_CLIENT_SECRET=  # AppSecret / Client Secret
   FOS_DINGTALK_LUNENG_ROBOT_CODE=     # 机器人编码，留空等于 Client ID
   FOS_DINGTALK_LUNENG_CARD_TEMPLATE_ID=  # 可选
   ```
4. 启动长连接：`fos channels run`（docker compose 里是 `channels` 服务）。
5. 绑定账号：`fos bind-im luneng <login> dingtalk <钉钉 userid>`，或 `PUT /v1/channels/identities`。
   通讯录同步（自动绑定）放在后续 PR。
6. 把机器人拉进车间群，@它发一张照片，应收到“已收到并识别”卡片。

### 互动卡片模板约定（可选）

在钉钉卡片平台建一个模板，变量：

| 变量 | 内容 |
|---|---|
| `title` | 卡片标题 |
| `content` | Markdown 正文（字段、模拟数据提示都已渲染在里面） |
| `buttons` | JSON 数组 `[{"id","label","style"}]`，最多 3 个 |
| `status` | `open`；处理后可用于隐藏按钮 |

按钮的回调类型选“回传请求”（Stream），回调参数里带 `action`=按钮 id（`confirm` / `cancel`）。

### 需要用真实应用核对的地方

以下字段按钉钉开放平台文档实现，测试用的是打桩数据，接入真实应用时逐项核对：

- Stream 建连 `POST /v1.0/gateway/connections/open` 与 ping / ack 帧格式；
- 机器人消息字段：`senderStaffId`、`conversationType`、`isInAtList`、图片 `downloadCode`；
- 发消息 `robot/groupMessages/send`、`robot/oToMessages/batchSend` 的 `msgKey` 与 `msgParam`；
- 互动卡片 `card/instances/createAndDeliver` 的 `openSpaceId` 格式和回调里的 `cardPrivateData`。

## 开发与演示：模拟信道

非 prod 环境下有一个模拟信道，消息走与钉钉完全相同的 Trigger 入口：

```bash
# 绑定 IM 账号（管理员）
curl -X PUT localhost:8000/v1/channels/identities -H "Authorization: Bearer $KEY" \
  -d '{"channel":"mock","external_id":"im-wh","login":"warehouse"}' -H 'content-type: application/json'
# 模拟仓管在群里发消息
curl -X POST localhost:8000/v1/channels/mock/messages -H "Authorization: Bearer $KEY" \
  -d '{"external_msg_id":"m1","sender_id":"im-wh","text":"确认 K7Q2M"}' -H 'content-type: application/json'
# 看发出去的消息和卡片
curl localhost:8000/v1/channels/mock/outbox -H "Authorization: Bearer $KEY"
```

## 权限

| 权限 | 用途 |
|---|---|
| `platform.identity:write` | 绑定 / 解绑 IM 账号 |
| `platform.card:write` / `:read` | 发卡片 / 查卡片状态 |
| `platform.trigger:read` | 查看 Trigger 记录 |
| `platform.channel:write` | 使用模拟信道（仅非 prod） |

试点租户里这些只有管理员有（`*.*:*`）。

## 后续

- 飞书、企业微信适配器（同一接口：`send_text` / `send_card` / `fetch_attachment` + 各自的收消息入口）；
- 通讯录同步自动绑定账号；钉钉卡片处理后更新卡片状态；
- Router（W7–W8）接手 `route=router` 的 Trigger，Slow Path 由模型判断；
- 文档解析改为异步任务，大文件不阻塞消息处理。
