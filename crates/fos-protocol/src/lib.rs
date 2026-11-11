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
    /// 模拟数据（仪表模拟器、演示）。云端拒绝把模拟读数写进正式测点（规则 4）
    #[serde(default, skip_serializing_if = "is_false")]
    pub mock: bool,
}

fn is_false(b: &bool) -> bool {
    !*b
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
    /// 厂端状态。只在云端用 `ReportStatus` 要求时附带
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<EdgeStatus>,
    /// 上一批回执里命令的执行结果（含验签被拒的），云端据此关闭命令
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub command_results: Vec<CommandResult>,
}

/// 一条命令在厂端的结果。`ok = false` 包括验签被拒、执行出错。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommandResult {
    pub command_id: String,
    pub ok: bool,
    /// 给人看的说明，例如“重新排队 12 批”“签名无效”
    pub detail: String,
}

/// 厂端状态：版本、缓存积压、各点位健康。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EdgeStatus {
    pub version: String,
    /// 还没上行成功的批次数、读数条数
    pub pending_batches: u64,
    pub pending_readings: u64,
    /// 最早一批未上行数据的生成时间（Unix 毫秒）；没有积压为空
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub oldest_pending_ms: Option<i64>,
    /// 被云端判为格式错误、暂存待查的批次数
    pub rejected_batches: u64,
    pub points: Vec<PointHealth>,
}

/// 一个点位的采集健康。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PointHealth {
    pub point: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_ok_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    pub consecutive_errors: u32,
}

/// 云端对一批上行的回执。`commands` 是顺带下发的签名命令（厂端不开入站端口，命令只能随回执下来）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UplinkAck {
    pub duplicate: bool,
    pub seq: u64,
    pub accepted: u64,
    /// 被拒的点位 → 原因（未登记、单位不符、模拟读数写正式测点等）
    #[serde(default)]
    pub rejected: std::collections::BTreeMap<String, String>,
    #[serde(default)]
    pub commands: Vec<SignedCommand>,
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

/// 带签名的命令。签名算法 Ed25519：云端私钥只在 KMS / 环境变量里，厂端配置里只有公钥。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SignedCommand {
    pub protocol: u16,
    pub command_id: String,
    pub tenant_id: String,
    pub edge_id: String,
    /// 过期时间，Unix 毫秒；过期命令一律拒绝
    pub expires_ms: i64,
    pub command: EdgeCommand,
    /// 对 [`SignedCommand::signing_bytes`] 的 Ed25519 签名（标准 base64）
    pub signature: String,
}

#[derive(Serialize)]
struct Unsigned<'a> {
    protocol: u16,
    command_id: &'a str,
    tenant_id: &'a str,
    edge_id: &'a str,
    expires_ms: i64,
    command: &'a EdgeCommand,
}

impl SignedCommand {
    /// 被签名的字节：除 `signature` 外的字段按声明顺序输出的紧凑 JSON（UTF-8，不转义中文）。
    ///
    /// Python 侧等价写法：
    /// `json.dumps({"protocol":…, "command_id":…, "tenant_id":…, "edge_id":…, "expires_ms":…,
    /// "command": {"type": …, …}}, ensure_ascii=False, separators=(",", ":")).encode()`
    pub fn signing_bytes(&self) -> Vec<u8> {
        serde_json::to_vec(&Unsigned {
            protocol: self.protocol,
            command_id: &self.command_id,
            tenant_id: &self.tenant_id,
            edge_id: &self.edge_id,
            expires_ms: self.expires_ms,
            command: &self.command,
        })
        .expect("命令序列化不会失败")
    }
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
            mock: false,
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
    fn mock_flag_only_when_set() {
        let j = r#"{"point":"a","ts_ms":1,"value":1.5,"unit":"℃","source":"direct","quality":"good","mock":true}"#;
        let r: Reading = serde_json::from_str(j).unwrap();
        assert!(r.mock);
        assert_eq!(serde_json::to_string(&r).unwrap(), j);
    }

    #[test]
    fn signing_bytes_are_canonical() {
        let c = SignedCommand {
            protocol: 1,
            command_id: "c1".into(),
            tenant_id: "luneng".into(),
            edge_id: "e1".into(),
            expires_ms: 5,
            command: EdgeCommand::Resend { from_seq: 9 },
            signature: "x".into(),
        };
        assert_eq!(
            String::from_utf8(c.signing_bytes()).unwrap(),
            r#"{"protocol":1,"command_id":"c1","tenant_id":"luneng","edge_id":"e1","expires_ms":5,"command":{"type":"resend","from_seq":9}}"#
        );
    }

    #[test]
    fn ack_without_commands_parses() {
        let a: UplinkAck = serde_json::from_str(
            r#"{"duplicate":false,"seq":3,"accepted":2,"rejected":{"x":"测点未登记"}}"#,
        )
        .unwrap();
        assert_eq!(a.seq, 3);
        assert!(a.commands.is_empty());
    }

    #[test]
    fn results_only_when_present() {
        let b = UplinkBatch {
            protocol: 1,
            tenant_id: "luneng".into(),
            edge_id: "e1".into(),
            seq: 1,
            backfill: false,
            readings: vec![],
            status: None,
            command_results: vec![],
        };
        let j = serde_json::to_string(&b).unwrap();
        assert!(!j.contains("command_results"));
        let mut b2 = b.clone();
        b2.command_results.push(CommandResult {
            command_id: "c1".into(),
            ok: true,
            detail: "完成".into(),
        });
        let back: UplinkBatch = serde_json::from_str(&serde_json::to_string(&b2).unwrap()).unwrap();
        assert_eq!(back, b2);
    }

    /// 云端（Python）签发的命令：同一份向量在 tests/test_edge_commands.py 里由 Python 生成并比对，
    /// 两边对“被签名的字节”理解一致。
    #[test]
    fn cloud_vector_signing_bytes() {
        let c: SignedCommand =
            serde_json::from_str(include_str!("../testdata/signed_command.json")).unwrap();
        assert_eq!(
            String::from_utf8(c.signing_bytes()).unwrap(),
            r#"{"protocol":1,"command_id":"cmd-测试-1","tenant_id":"luneng","edge_id":"edge-1","expires_ms":1762000300000,"command":{"type":"read_now","points":["k1.top","窑顶"]}}"#
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
