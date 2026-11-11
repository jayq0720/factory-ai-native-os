//! fos-edge：厂端 daemon（方案 3.2、7.2）。
//!
//! - 采集：Modbus RTU（RS485 串口）/ Modbus TCP，每条链路一个任务；仪表参数全在 edge.toml；
//! - 缓存：读数先封批、编号、写进本地 SQLite，再上行；断网时一直攒（默认保留 7 天），恢复后按序号补传；
//! - 上行：HTTPS POST `/v1/edge/uplink`（API Key 走环境变量），云端按 (edge_id, seq) 去重；
//! - 命令：只随上行回执下来，Ed25519 验签，协议里只有只读命令；
//! - 模拟器：`fos-edge simulate` 起一个 Modbus TCP 仪表模拟器，现场调研前先跑通整条链路。

pub mod buffer;
pub mod commands;
pub mod config;
pub mod daemon;
pub mod modbus;
pub mod sim;
pub mod uplink;
