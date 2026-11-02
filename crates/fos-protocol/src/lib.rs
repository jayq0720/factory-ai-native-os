//! fos-protocol：云端与厂端（fos-edge）共享的数据契约。
//!
//! 设计要点（方案 3.2、7.2）：
//! - 同一个窑可以混用三条接入路径，读数进入同一张时序表，只是 `source` 和 `quality` 不同；
//! - 云端下发给厂端的只有**白名单里的只读命令**，厂端校验签名后执行；
//! - JSON 字段名稳定，Python 侧按同一份结构解析（后续由此生成 Python 类型）。

use serde::{Deserialize, Serialize};

/// 协议版本。不兼容变更时递增。
pub const PROTOCOL_VERSION: u16 = 1;

/// 数据从哪条路径来（方案 3.2 的 A / B / C）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourcePath {
    /// A：仪表/PLC 直接联网（Modbus RTU/TCP、OPC UA、S7）
    Direct,
    /// B：旁路加装传感器或采集模块
    Retrofit,
    /// C：拍照/摄像头读数或人工报数
    Manual,
}

/// 采集驱动类型。新驱动只加枚举值和实现，不改契约其他部分。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum DriverKind {
    ModbusRtu,
    ModbusTcp,
    Opcua,
    S7,
    CameraSnapshot,
    Manual,
}

impl DriverKind {
    /// 驱动对应的默认数据路径。旁路改造的 Modbus 采集模块在点位配置里显式标 `retrofit`。
    pub fn default_path(self) -> SourcePath {
        match self {
            DriverKind::ModbusRtu | DriverKind::ModbusTcp | DriverKind::Opcua | DriverKind::S7 => {
                SourcePath::Direct
            }
            DriverKind::CameraSnapshot | DriverKind::Manual => SourcePath::Manual,
        }
    }
}

/// 读数质量。断网补传、视觉读数置信度低等情况都要如实标出。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Quality {
    Good,
    /// 视觉/OCR 读数，附置信度
    Estimated,
    /// 通讯失败、超量程等
    Bad,
}

/// 一个点位的一次读数。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Reading {
    /// 点位 id，租户内唯一，例如 "kiln1.zone18.temp"
    pub point: String,
    /// 采样时间，Unix 毫秒，UTC
    pub ts_ms: i64,
    pub value: f64,
    pub unit: String,
    pub source: SourcePath,
    pub quality: Quality,
    /// 视觉读数的置信度 0–1；其他路径为空
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f32>,
    /// 证据引用，例如照片在 OSS 的对象键；直接采集为空
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub evidence: Option<String>,
}

/// 厂端上行的一批读数。`seq` 单调递增，云端按 (edge_id, seq) 去重，断网补传不会重复入库。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UplinkBatch {
    pub protocol: u16,
    pub tenant_id: String,
    pub edge_id: String,
    pub seq: u64,
    /// 本批是否是断网后的补传
    #[serde(default)]
    pub backfill: bool,
    pub readings: Vec<Reading>,
}

/// 云端可以下发的命令：**只有只读命令**。写设备、改设定值不在协议里，从根上禁止。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum EdgeCommand {
    /// 立即读一次指定点位
    ReadNow { points: Vec<String> },
    /// 上报厂端状态（版本、缓存积压、各驱动健康）
    ReportStatus,
    /// 补传某个序号之后的缓存数据
    Resend { from_seq: u64 },
}

/// 带签名的命令。签名算法与密钥轮换在 fos-edge v0.1（W19+）实现；这里先固定结构。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SignedCommand {
    pub protocol: u16,
    pub command_id: String,
    pub tenant_id: String,
    pub edge_id: String,
    /// 过期时间，Unix 毫秒；过期命令一律拒绝
    pub expires_ms: i64,
    pub command: EdgeCommand,
    /// 对以上字段规范化 JSON 的签名（base64）
    pub signature: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reading_json_is_stable() {
        let r = Reading {
            point: "kiln1.zone18.temp".into(),
            ts_ms: 1_762_000_000_000,
            value: 1298.5,
            unit: "℃".into(),
            source: SourcePath::Direct,
            quality: Quality::Good,
            confidence: None,
            evidence: None,
        };
        let j = serde_json::to_string(&r).unwrap();
        assert_eq!(
            j,
            r#"{"point":"kiln1.zone18.temp","ts_ms":1762000000000,"value":1298.5,"unit":"℃","source":"direct","quality":"good"}"#
        );
        let back: Reading = serde_json::from_str(&j).unwrap();
        assert_eq!(back, r);
    }

    #[test]
    fn manual_reading_carries_evidence() {
        let j = r#"{"point":"kiln3.temp","ts_ms":1,"value":1120,"unit":"℃","source":"manual",
                    "quality":"estimated","confidence":0.91,"evidence":"oss://photos/k3/0001.jpg"}"#;
        let r: Reading = serde_json::from_str(j).unwrap();
        assert_eq!(r.source, SourcePath::Manual);
        assert_eq!(r.confidence, Some(0.91));
    }

    #[test]
    fn commands_are_tagged_and_write_is_impossible() {
        let c: EdgeCommand = serde_json::from_str(r#"{"type":"read_now","points":["a"]}"#).unwrap();
        assert_eq!(
            c,
            EdgeCommand::ReadNow {
                points: vec!["a".into()]
            }
        );
        assert!(
            serde_json::from_str::<EdgeCommand>(r#"{"type":"write_register","addr":1}"#).is_err()
        );
    }

    #[test]
    fn driver_default_paths() {
        assert_eq!(DriverKind::ModbusRtu.default_path(), SourcePath::Direct);
        assert_eq!(
            DriverKind::CameraSnapshot.default_path(),
            SourcePath::Manual
        );
    }
}
