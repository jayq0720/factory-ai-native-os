# fos-edge v0.1：厂端采集（W20，里程碑 M4）

对应开发方案 3.2“窑炉与设备接入：三条路径”、7.2 安全边界。云端部分（读数入库、告警、抄表提醒）见 `docs/kiln-monitoring.md`。

fos-edge 是跑在工控机 / 网关盒子上的一个 Rust 程序：连仪表采数，先写本地缓存再上行；断网时一直攒，恢复后按序号补传；云端只能随上行回执下发**签名的只读命令**。

## 三条路径在厂端怎么落

| 路径 | 厂端做什么 | 配置 |
|---|---|---|
| A 直接联网（仪表 RS485 / PLC 以太网） | fos-edge 读寄存器 | `driver = "modbus-rtu"` / `"modbus-tcp"` |
| B 旁路加装（热电偶 + 采集模块、带通讯的电表） | 同上，只是数据标 `retrofit` | 同上 + `source = "retrofit"` |
| C 不能联网（拍照、抄表） | **不采**，云端按测点间隔提醒抄表 | `driver = "manual"`（写上便于对照，运行时跳过） |

OPC UA、S7、摄像头定时拍仪表盘的驱动还没做，配置里可以先写，运行时跳过并提示。**仪表型号、地址、寄存器表全部在 edge.toml 里**，代码不认识任何具体型号；现场调研后只改配置。

## 数据流

```
仪表 / PLC ──Modbus──▶ 链路任务（一个串口 / 一台 TCP 设备一个，点位依次读）
                         │ 读数（时间戳、值、质量、来源、mock 标记）
                         ▼
                      封批任务：每 flush_s 秒封一批；没读数也每 heartbeat_s 秒封一个空批次当心跳
                         │ 分配序号 seq、写 SQLite（同一事务）
                         ▼
                      上行任务：按 seq 从小到大 POST /v1/edge/uplink
                         ├─ 成功 / 云端判重复  → 标记已发
                         ├─ 400 / 413 / 422     → 标记被拒，暂存待查，不阻塞后面
                         └─ 断网 / 5xx / 401 / 403 → 留在缓存，退避重试（2 秒 → 5 分钟）
```

- **不丢**：读数先落盘再上行；进程退出时把内存里的读数落盘；断电最多丢最后几秒。
- **不重**：序号在落盘时分配、补传时不变，云端按 `(edge_id, seq)` 去重。新建缓存文件时序号从当前 Unix 毫秒起步，缓存文件丢了或换了工控机，新序号也一定比旧的大，不会被云端当成重复而悄悄丢弃。
- **补传标记**：生成后超过 3 个封批周期才发出去的批次带 `backfill = true`。
- **保留期**：未上行的保留 `buffer_days`（至少 7 天，超期丢弃并记错误日志）；已上行的留 1 天备查；被拒的留 `buffer_days`，`fos-edge resend` 可重新排队。
- **断网和设备故障分得开**：网关在线时心跳批次照常到，云端看到“网关在、某个点位没数 / 质量差”判设备故障；整台网关没心跳判断网（`docs/kiln-monitoring.md` 的 `edge_offline` / `point_fault`）。
- **故障值**：仪表表示断偶 / 超量程的原始值写在 `bad_values`（例如 32767、-1999），命中时读数照传，质量记 `bad`。

## 配置（edge.toml）

完整示例：`crates/fos-edge/edge.example.toml`（六安绿能，占位值）、`crates/fos-edge/edge.sim.toml`（连模拟器）。

| 键 | 默认 | 说明 |
|---|---|---|
| tenant_id / edge_id | — | edge_id 要和云端测点登记的“采集网关”一致 |
| uplink_url | — | `https://…/v1/edge/uplink`；本机联调允许 `http://127.0.0.1` / `http://localhost` |
| api_key_env | FOS_EDGE_API_KEY | 上行 API Key 所在的环境变量（Key 不写进配置，规则 2） |
| mode | real | `mock`：每条读数带 mock 标记，云端只接受写进模拟测点（规则 4） |
| buffer_days / buffer_path | 7 / fos-edge-buffer.db | 相对路径按配置文件所在目录解析 |
| flush_s / heartbeat_s / max_batch | 10 / 60 / 1000 | 封批周期、心跳、单批上限（云端上限 5000） |
| command_public_key | 空 | 云端命令验签公钥；不填则拒绝一切命令 |

点位 `[[point]]`：`id`（= 云端测点编号）、`driver`、`unit`（要和测点登记一致）、`source`、`interval_s`、`params`。

Modbus `params`（拼错键名直接报错）：

| 键 | 默认 | 说明 |
|---|---|---|
| address | 1 | 从站地址（仪表通讯地址） |
| register | 必填 | 起始寄存器（0 起，协议地址） |
| function | holding | `holding`（03）或 `input`（04）；只有读功能码 |
| data_type | i16 | `u16` / `i16` / `u32` / `i32` / `f32` |
| word_order | big | 32 位值高字在前（`big`）还是低字在前（`little`） |
| scale / offset | 1 / 0 | 工程值 = 原始值 × scale + offset |
| bad_values | [] | 表示断偶 / 超量程的原始值 |
| timeout_ms | 1000 | 单次读超时 |
| host / port | — / 502 | Modbus TCP |
| port / baud / parity / data_bits / stop_bits | — / 9600 / none / 8 / 1 | Modbus RTU（port 是串口路径）；同一串口上的通讯参数必须一致 |

## 命令

```bash
cargo build --release -p fos-edge                     # 产物 target/release/fos-edge
fos-edge check edge.toml                               # 校验配置，列出每个点位怎么采
FOS_EDGE_API_KEY=fos_xxx fos-edge run edge.toml        # 采集 + 缓存 + 上行（日志级别 FOS_EDGE_LOG=debug）
fos-edge status edge.toml                              # 积压多少、最早积压多久、被拒批次
fos-edge resend edge.toml --from <seq>                 # 把某个序号起的批次重新排队
fos-edge simulate --listen 127.0.0.1:5020 --speed 60   # 仪表模拟器
```

工控机常驻用 systemd：`deploy/edge/fos-edge.service`（文件头有安装步骤）。上行 Key 给 `edge` 角色的用户发（一台网关一个用户），它只有 `production.reading:write`。

## 仪表模拟器（现场调研前先跑通）

`fos-edge simulate` 起一个 Modbus TCP 从站：温度按“升温 12 小时 → 保温 4 小时 → 降温 20 小时”的曲线走（`--speed` 倍速、`--start-h` 从第几小时开始），电表累计走字，`--fault-unit N` 让某个从站返回断偶值。寄存器表见 `crates/fos-edge/src/sim.rs` 文件头。只响应读请求。

本机联调整条链路（全部是模拟数据）：

```bash
export FOS_DATABASE_URL=sqlite+aiosqlite:///./fos.db
fos quickstart                                         # 管理员 Key
fos create-user luneng edge-demo 采集网关 --roles edge && fos issue-key luneng edge-demo
fos serve &                                            # 网页 /app/kiln.html 点“加载演示数据”（或 POST /v1/kiln/demo）
fos-edge simulate --start-h 10 &
FOS_EDGE_API_KEY=<edge 的 Key> fos-edge run crates/fos-edge/edge.sim.toml
```

先启动 fos-edge、后启动云端也行：期间的读数在本地缓存里，云端起来后自动补传（带 `backfill`）。

## 签名只读命令（厂端 W20 + 云端 W21）

协议里只有三条命令：`read_now`（立即读指定点位）、`report_status`（下一批附带厂端状态：版本、积压、被拒批次、各点位健康）、`resend`（从某序号起重新排队补传）。写寄存器、改设定值在协议里不存在（规则 7），云端接口收到别的 `type` 直接 422。

```
人（网页 / POST /v1/edge/commands，权限 production.edge:write）        Watchdog（每 status_every_minutes 自动要一次状态）
        │ 排队：edge_commands 表，status = pending，queue_minutes 内有效
        ▼
网关下次上行 POST /v1/edge/uplink ──▶ 回执 UplinkAck.commands：下发时才用私钥签名，有效期 command_ttl_s（默认 5 分钟）
        │                                         status = sent
        ▼
厂端验签（公钥在 edge.toml）→ 执行 → 结果放进下一批的 command_results（验签被拒的也报，例如“签名无效”）
        │
        ▼
云端收结果：done / failed；签名过期还没结果 → 同一个 id 重签再带一次；排队超过 queue_minutes → expired
```

- **签名**：Ed25519，被签名的是除 `signature` 外字段的紧凑 JSON（`SignedCommand::signing_bytes`；Python 是 `fos/monitoring/edge.py` 的 `signing_bytes`）。两边对同一份测试向量 `crates/fos-protocol/testdata/signed_command.json` 逐字节比对，Rust 用它验签（`verifies_cloud_vector`），Python 用它比对签名结果（`tests/test_edge_commands.py`）。
- **厂端校验**：租户 / 网关匹配、未过期、有效期不超过 1 小时、同一 `command_id` 只执行一次。执行过的 id 记到“过期后再 1 小时”，云端重签同一条命令时厂端只回报上次的结果，不会执行两遍（`queue_minutes` 最大 60，保证重签都落在这个窗口里）。网关重启后记录清空，最坏情况是一条只读命令执行两次，没有副作用。
- **结果不丢**：结果和状态跟着批次走；这一批没发出去（断网 / 被拒）就放回去，下一批再带。
- **私钥只在云端**：环境变量 `FOS_EDGE_COMMAND_KEY`（32 字节种子的 base64，或 PEM 全文；生产放 KMS / 凭据管家，规则 2）。没配置时云端不能下发命令（接口 409），回执里没有命令，采集照常。
- **密钥生成**：`fos edge-keygen` 打印一行私钥（放环境变量）和一行公钥（填 edge.toml 的 `command_public_key`）；网页“采集网关”一节也显示当前公钥。也可以用 openssl：`openssl genpkey -algorithm ed25519 -out cmd.pem`，公钥 base64：`openssl pkey -in cmd.pem -pubout -outform DER | tail -c 32 | base64`，私钥种子：`openssl pkey -in cmd.pem -outform DER | tail -c 32 | base64`。
- 一个云端私钥服务所有租户：租户 id 在被签名的字节里，跨租户重放会被厂端拒绝。以后要分租户密钥，再按租户从 KMS 取。

### 接口

| 接口 | 权限 | 说明 |
|---|---|---|
| `GET /v1/edge/gateways` | production.edge:read | 每台网关：在线、最后上行、测点、最近一次状态及解读（积压分钟、被拒批次、连续失败的点位）、最近 5 条命令；以及云端是否能签名、公钥 |
| `POST /v1/edge/commands` | production.edge:write | `{"edge_id": "edge-1", "command": {"type": "read_now", "points": ["k1.top"]}}`；点位必须是登记在这台网关上的 |
| `GET /v1/edge/commands?edge_id=` | production.edge:read | 命令记录：谁要的、下发几次、结果 |
| `POST /v1/edge/commands/{id}/cancel` | production.edge:write | 只能取消还没下发的 |

网关自己的 Key（`edge` 角色，只有 `production.reading:write`）不能给自己下命令。命令和状态不是业务事实，不走账本；谁要的、结果如何都记在 `edge_commands` 表里备查（迁移 0011）。

### 厂端状态与 Watchdog

状态存在那一批的 `edge_batches.status`。Watchdog（`fos channels run` 里的监控判定）看网关在线时最近一次状态（不超过两个上报周期）：

- 积压最早一批超过 `edge.backlog_minutes`（默认 30 分钟）还没传上来 → 告警 `edge_backlog`；
- 有被云端拒收、暂存在网关上的批次 → 告警 `edge_rejected`，查明原因后在网页点“重新补传”；
- 状态恢复正常（下一次上报）自动关闭。配置在 `packs/<pack>/monitoring.yaml` 的 `edge:` 段。

## 待现场确认（占位值）

- 每座窑仪表的型号、通讯口、从站地址、波特率、寄存器表、倍率、故障值（整理进 edge.toml）；
- 工控机的系统（默认 Linux x86_64 / ARM64）、上网方式（有线 / 4G 路由器）；
- 电表 / 燃气表是否带 RS485。

## 下一步

- OPC UA、S7、摄像头定时拍仪表盘驱动（按现场设备决定先做哪个）；
- 质量体系、另外两种 IM（W21–W22）。
