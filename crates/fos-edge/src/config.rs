//! 厂端配置（edge.toml）。点位与接入路径属于租户配置，部署时下发到工控机。
//!
//! 仪表型号、通讯参数、寄存器表都是现场调研后才知道的，全部写在配置里，代码不认识任何具体型号。
//! 密钥不在这里：上行用的 API Key 从环境变量读（`api_key_env`），命令验签只放公钥。

use std::collections::{HashMap, HashSet};

use fos_protocol::{DriverKind, SourcePath};
use serde::Deserialize;

use crate::modbus::{Link, ModbusPoint};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum DataMode {
    /// 真实仪表
    #[default]
    Real,
    /// 仪表模拟器 / 演示：每条读数带 `mock = true`，云端不让它进正式测点
    Mock,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EdgeConfig {
    pub tenant_id: String,
    pub edge_id: String,
    /// 云端上行地址，例如 https://fos.example.com/v1/edge/uplink
    pub uplink_url: String,
    /// 存放上行 API Key 的环境变量名（Key 本身不写进配置）
    #[serde(default = "default_api_key_env")]
    pub api_key_env: String,
    #[serde(default)]
    pub mode: DataMode,
    /// 断网缓存保留天数（方案 3.2：默认至少 7 天）
    #[serde(default = "default_buffer_days")]
    pub buffer_days: u32,
    /// 本地缓存文件（SQLite）
    #[serde(default = "default_buffer_path")]
    pub buffer_path: String,
    /// 多久把采到的读数封成一批（秒）
    #[serde(default = "default_flush_s")]
    pub flush_s: u32,
    /// 没有读数时也至少这么久发一次空批次当心跳（秒），云端靠它区分“断网”和“设备故障”
    #[serde(default = "default_heartbeat_s")]
    pub heartbeat_s: u32,
    /// 一批最多多少条读数（云端上限 5000）
    #[serde(default = "default_max_batch")]
    pub max_batch: usize,
    /// 云端命令验签公钥（Ed25519，32 字节，标准 base64）。不填则拒绝一切命令
    #[serde(default)]
    pub command_public_key: Option<String>,
    #[serde(default, rename = "point")]
    pub points: Vec<PointConfig>,
}

fn default_api_key_env() -> String {
    "FOS_EDGE_API_KEY".into()
}
fn default_buffer_days() -> u32 {
    7
}
fn default_buffer_path() -> String {
    "fos-edge-buffer.db".into()
}
fn default_flush_s() -> u32 {
    10
}
fn default_heartbeat_s() -> u32 {
    60
}
fn default_max_batch() -> usize {
    1000
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PointConfig {
    pub id: String,
    pub driver: DriverKind,
    pub unit: String,
    /// 不填则按驱动推断；旁路改造的采集模块显式写 "retrofit"
    pub source: Option<SourcePath>,
    /// 采样间隔（秒）。路径 C 的点位由云端按测点登记的间隔提醒抄表，fos-edge 不采
    pub interval_s: u32,
    /// 驱动参数，例如 Modbus 的 address / register / scale
    #[serde(default)]
    pub params: toml::Table,
}

impl PointConfig {
    pub fn source_path(&self) -> SourcePath {
        self.source.unwrap_or_else(|| self.driver.default_path())
    }

    /// fos-edge v0.1 能采的点位：Modbus RTU / TCP。
    pub fn collected(&self) -> bool {
        matches!(self.driver, DriverKind::ModbusRtu | DriverKind::ModbusTcp)
    }

    /// 解析 Modbus 参数（只对 Modbus 点位有意义）。
    pub fn modbus(&self) -> Result<ModbusPoint, String> {
        ModbusPoint::from_params(self.driver, &self.params)
            .map_err(|e| format!("点位 {}：{e}", self.id))
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("配置解析失败：{0}")]
    Parse(#[from] toml::de::Error),
    #[error("配置无效：{0}")]
    Invalid(String),
}

impl EdgeConfig {
    pub fn parse(text: &str) -> Result<Self, ConfigError> {
        let cfg: EdgeConfig = toml::from_str(text)?;
        cfg.validate()?;
        Ok(cfg)
    }

    /// v0.1 还没有驱动的点位（OPC UA、S7、摄像头），配置可以先写上，运行时跳过并提示。
    pub fn skipped(&self) -> impl Iterator<Item = &PointConfig> {
        self.points
            .iter()
            .filter(|p| !p.collected() && p.driver != DriverKind::Manual)
    }

    fn validate(&self) -> Result<(), ConfigError> {
        let bad = |m: String| Err(ConfigError::Invalid(m));
        if self.tenant_id.is_empty() || self.edge_id.is_empty() {
            return bad("tenant_id 和 edge_id 不能为空".into());
        }
        if self.buffer_days < 7 {
            return bad(format!("buffer_days 至少 7 天，当前 {}", self.buffer_days));
        }
        if !uplink_url_ok(&self.uplink_url) {
            return bad(
                "uplink_url 必须是 https://（本机联调可用 http://127.0.0.1 或 http://localhost）"
                    .into(),
            );
        }
        if self.flush_s == 0 || self.heartbeat_s < self.flush_s {
            return bad("flush_s 必须大于 0，heartbeat_s 不能小于 flush_s".into());
        }
        if self.max_batch == 0 || self.max_batch > 5000 {
            return bad("max_batch 必须在 1–5000 之间（云端单批上限 5000）".into());
        }
        if let Some(k) = &self.command_public_key {
            crate::commands::parse_public_key(k).map_err(ConfigError::Invalid)?;
        }
        let mut seen = HashSet::new();
        // 同一个串口上所有仪表的波特率、校验位必须一致
        let mut serial: HashMap<String, Link> = HashMap::new();
        for p in &self.points {
            if !seen.insert(p.id.as_str()) {
                return bad(format!("点位 id 重复：{}", p.id));
            }
            if p.interval_s == 0 {
                return bad(format!("点位 {} 的 interval_s 不能为 0", p.id));
            }
            if p.collected() {
                let m = p.modbus().map_err(ConfigError::Invalid)?;
                if let Link::Rtu { path, .. } = &m.link {
                    if let Some(prev) = serial.get(path) {
                        if prev != &m.link {
                            return bad(format!(
                                "点位 {}：串口 {path} 上的通讯参数和其他点位不一致",
                                p.id
                            ));
                        }
                    } else {
                        serial.insert(path.clone(), m.link.clone());
                    }
                }
            }
        }
        Ok(())
    }
}

fn uplink_url_ok(url: &str) -> bool {
    if url.starts_with("https://") {
        return true;
    }
    ["http://127.0.0.1", "http://localhost"].iter().any(|p| {
        url.strip_prefix(p)
            .is_some_and(|rest| rest.is_empty() || rest.starts_with([':', '/']))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = include_str!("../edge.example.toml");
    const SIM: &str = include_str!("../edge.sim.toml");

    #[test]
    fn sample_config_is_valid() {
        let c = EdgeConfig::parse(SAMPLE).unwrap();
        assert_eq!(c.tenant_id, "luneng");
        assert_eq!(c.buffer_days, 7);
        assert_eq!(c.mode, DataMode::Real);
        let paths: Vec<_> = c.points.iter().map(|p| p.source_path()).collect();
        assert_eq!(
            paths,
            vec![SourcePath::Direct, SourcePath::Retrofit, SourcePath::Manual]
        );
    }

    #[test]
    fn sim_config_is_valid_and_mock() {
        let c = EdgeConfig::parse(SIM).unwrap();
        assert_eq!(c.mode, DataMode::Mock);
        assert!(c.points.iter().all(|p| p.collected()));
    }

    #[test]
    fn rejects_short_buffer_and_plain_http() {
        let base = SAMPLE.replace("buffer_days = 7", "buffer_days = 3");
        assert!(matches!(
            EdgeConfig::parse(&base),
            Err(ConfigError::Invalid(_))
        ));
        let http = SAMPLE.replace("https://", "http://");
        assert!(matches!(
            EdgeConfig::parse(&http),
            Err(ConfigError::Invalid(_))
        ));
        assert!(uplink_url_ok("http://127.0.0.1:8000/v1/edge/uplink"));
        assert!(!uplink_url_ok("http://127.0.0.1.evil.com/x"));
    }

    #[test]
    fn rejects_modbus_without_register() {
        let t = r#"
            tenant_id = "t"
            edge_id = "e"
            uplink_url = "https://x"
            [[point]]
            id = "a"
            driver = "modbus-rtu"
            unit = "℃"
            interval_s = 1
            params = { port = "/dev/ttyUSB0" }
        "#;
        let e = EdgeConfig::parse(t).unwrap_err().to_string();
        assert!(e.contains("register"), "{e}");
    }

    #[test]
    fn rejects_typo_in_params_and_conflicting_serial() {
        let typo = r#"
            tenant_id = "t"
            edge_id = "e"
            uplink_url = "https://x"
            [[point]]
            id = "a"
            driver = "modbus-tcp"
            unit = "℃"
            interval_s = 1
            params = { host = "10.0.0.2", register = 1, scael = 0.1 }
        "#;
        assert!(EdgeConfig::parse(typo).is_err());
        let serial = r#"
            tenant_id = "t"
            edge_id = "e"
            uplink_url = "https://x"
            [[point]]
            id = "a"
            driver = "modbus-rtu"
            unit = "℃"
            interval_s = 1
            params = { port = "/dev/ttyUSB0", baud = 9600, register = 1 }
            [[point]]
            id = "b"
            driver = "modbus-rtu"
            unit = "℃"
            interval_s = 1
            params = { port = "/dev/ttyUSB0", baud = 19200, register = 2 }
        "#;
        let e = EdgeConfig::parse(serial).unwrap_err().to_string();
        assert!(e.contains("串口"), "{e}");
    }
}
