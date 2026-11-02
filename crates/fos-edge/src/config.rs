//! 厂端配置（edge.toml）。点位与接入路径属于租户配置，部署时下发到工控机。

use std::collections::HashSet;

use fos_protocol::{DriverKind, SourcePath};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EdgeConfig {
    pub tenant_id: String,
    pub edge_id: String,
    /// 云端上行地址
    pub uplink_url: String,
    /// 断网缓存保留天数（方案 3.2：默认至少 7 天）
    #[serde(default = "default_buffer_days")]
    pub buffer_days: u32,
    #[serde(default, rename = "point")]
    pub points: Vec<PointConfig>,
}

fn default_buffer_days() -> u32 {
    7
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PointConfig {
    pub id: String,
    pub driver: DriverKind,
    pub unit: String,
    /// 不填则按驱动推断；旁路改造的采集模块显式写 "retrofit"
    pub source: Option<SourcePath>,
    /// 采样间隔（秒）。路径 C 是提醒拍照的间隔
    pub interval_s: u32,
    /// 驱动参数，例如 Modbus 的 address / register / scale
    #[serde(default)]
    pub params: toml::Table,
}

impl PointConfig {
    pub fn source_path(&self) -> SourcePath {
        self.source.unwrap_or_else(|| self.driver.default_path())
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

    fn validate(&self) -> Result<(), ConfigError> {
        let bad = |m: String| Err(ConfigError::Invalid(m));
        if self.buffer_days < 7 {
            return bad(format!("buffer_days 至少 7 天，当前 {}", self.buffer_days));
        }
        if !self.uplink_url.starts_with("https://") && !self.uplink_url.starts_with("mqtts://") {
            return bad("uplink_url 必须是 https:// 或 mqtts://".into());
        }
        let mut seen = HashSet::new();
        for p in &self.points {
            if !seen.insert(p.id.as_str()) {
                return bad(format!("点位 id 重复：{}", p.id));
            }
            if p.interval_s == 0 {
                return bad(format!("点位 {} 的 interval_s 不能为 0", p.id));
            }
            let needs_addr = matches!(p.driver, DriverKind::ModbusRtu | DriverKind::ModbusTcp);
            if needs_addr && !p.params.contains_key("register") {
                return bad(format!("Modbus 点位 {} 缺少 params.register", p.id));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = include_str!("../edge.example.toml");

    #[test]
    fn sample_config_is_valid() {
        let c = EdgeConfig::parse(SAMPLE).unwrap();
        assert_eq!(c.tenant_id, "luneng");
        assert_eq!(c.buffer_days, 7);
        let paths: Vec<_> = c.points.iter().map(|p| p.source_path()).collect();
        assert_eq!(
            paths,
            vec![SourcePath::Direct, SourcePath::Retrofit, SourcePath::Manual]
        );
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
        "#;
        assert!(matches!(EdgeConfig::parse(t), Err(ConfigError::Invalid(_))));
    }
}
