//! Modbus RTU / TCP 采集：点位参数、寄存器解码、按“总线”（一个串口或一个 TCP 设备）读数。
//!
//! 只用读功能码（03 读保持寄存器、04 读输入寄存器）；写寄存器的函数在这里一个都不调用（规则 7）。

use std::time::Duration;

use fos_protocol::DriverKind;
use serde::Deserialize;
use tokio_modbus::client::{rtu, tcp, Context, Reader};
use tokio_modbus::slave::SlaveContext;
use tokio_modbus::Slave;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Function {
    /// 03 读保持寄存器（多数温控仪表的测量值在这里）
    #[default]
    Holding,
    /// 04 读输入寄存器
    Input,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum DataType {
    U16,
    #[default]
    I16,
    U32,
    I32,
    F32,
}

impl DataType {
    pub fn words(self) -> u16 {
        match self {
            DataType::U16 | DataType::I16 => 1,
            _ => 2,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum WordOrder {
    /// 高字在前（ABCD）
    #[default]
    Big,
    /// 低字在前（CDAB），部分国产仪表用这种
    Little,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Parity {
    #[default]
    None,
    Even,
    Odd,
}

/// 点位配置里 `params` 的全部可用键。拼错的键直接报错，现场改配置时少踩坑。
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawParams {
    /// 从站地址（仪表通讯地址）
    #[serde(default = "one")]
    address: u8,
    register: Option<u16>,
    #[serde(default)]
    function: Function,
    #[serde(default)]
    data_type: DataType,
    #[serde(default)]
    word_order: WordOrder,
    #[serde(default = "unit_scale")]
    scale: f64,
    #[serde(default)]
    offset: f64,
    /// 仪表表示“断偶 / 超量程”的原始值，例如 32767、-1999；命中则质量记为 bad
    #[serde(default)]
    bad_values: Vec<f64>,
    #[serde(default = "default_timeout_ms")]
    timeout_ms: u64,
    // TCP
    host: Option<String>,
    // TCP 是端口号（默认 502），RTU 是串口路径
    port: Option<toml::Value>,
    // RTU
    #[serde(default = "default_baud")]
    baud: u32,
    #[serde(default)]
    parity: Parity,
    #[serde(default = "default_data_bits")]
    data_bits: u8,
    #[serde(default = "one")]
    stop_bits: u8,
}

fn one() -> u8 {
    1
}
fn unit_scale() -> f64 {
    1.0
}
fn default_timeout_ms() -> u64 {
    1000
}
fn default_baud() -> u32 {
    9600
}
fn default_data_bits() -> u8 {
    8
}

/// 物理链路。同一条链路上的点位由一个任务依次读（RS485 总线不能并发）。
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Link {
    Tcp {
        host: String,
        port: u16,
    },
    Rtu {
        path: String,
        baud: u32,
        parity: Parity,
        data_bits: u8,
        stop_bits: u8,
    },
}

impl Link {
    pub fn key(&self) -> String {
        match self {
            Link::Tcp { host, port } => format!("tcp://{host}:{port}"),
            Link::Rtu { path, .. } => format!("rtu://{path}"),
        }
    }
}

/// 读一个点位需要的全部信息。
#[derive(Debug, Clone, PartialEq)]
pub struct ModbusPoint {
    pub link: Link,
    pub slave: u8,
    pub register: u16,
    pub function: Function,
    pub data_type: DataType,
    pub word_order: WordOrder,
    pub scale: f64,
    pub offset: f64,
    pub bad_values: Vec<f64>,
    pub timeout: Duration,
}

impl ModbusPoint {
    pub fn from_params(driver: DriverKind, params: &toml::Table) -> Result<Self, String> {
        let raw: RawParams = toml::Value::Table(params.clone())
            .try_into()
            .map_err(|e: toml::de::Error| format!("params 有误：{}", e.message()))?;
        let register = raw.register.ok_or("缺少 params.register")?;
        let link = match driver {
            DriverKind::ModbusTcp => {
                let host = raw.host.ok_or("Modbus TCP 缺少 params.host")?;
                let port = match raw.port {
                    None => 502,
                    Some(toml::Value::Integer(p)) => {
                        u16::try_from(p).map_err(|_| format!("端口 {p} 无效"))?
                    }
                    Some(_) => return Err("Modbus TCP 的 params.port 应是端口号".into()),
                };
                Link::Tcp { host, port }
            }
            DriverKind::ModbusRtu => {
                let path = match raw.port {
                    Some(toml::Value::String(s)) if !s.is_empty() => s,
                    _ => {
                        return Err("Modbus RTU 缺少 params.port（串口，例如 /dev/ttyUSB0）".into())
                    }
                };
                if !(5..=8).contains(&raw.data_bits) || !(1..=2).contains(&raw.stop_bits) {
                    return Err("data_bits 应为 5–8，stop_bits 应为 1 或 2".into());
                }
                Link::Rtu {
                    path,
                    baud: raw.baud,
                    parity: raw.parity,
                    data_bits: raw.data_bits,
                    stop_bits: raw.stop_bits,
                }
            }
            other => return Err(format!("{other:?} 不是 Modbus 驱动")),
        };
        if raw.scale == 0.0 || !raw.scale.is_finite() {
            return Err("scale 不能为 0".into());
        }
        Ok(ModbusPoint {
            link,
            slave: raw.address,
            register,
            function: raw.function,
            data_type: raw.data_type,
            word_order: raw.word_order,
            scale: raw.scale,
            offset: raw.offset,
            bad_values: raw.bad_values,
            timeout: Duration::from_millis(raw.timeout_ms.clamp(100, 30_000)),
        })
    }

    /// 寄存器原始值 → (换算后的值, 是否命中故障值)。
    pub fn decode(&self, words: &[u16]) -> Result<(f64, bool), String> {
        let n = self.data_type.words() as usize;
        if words.len() < n {
            return Err(format!("寄存器个数不足：要 {n} 个，收到 {}", words.len()));
        }
        let (hi, lo) = match self.word_order {
            WordOrder::Big => (words[0], *words.get(1).unwrap_or(&0)),
            WordOrder::Little => (*words.get(1).unwrap_or(&0), words[0]),
        };
        let u32v = (u32::from(hi) << 16) | u32::from(lo);
        let raw = match self.data_type {
            DataType::U16 => f64::from(words[0]),
            DataType::I16 => f64::from(words[0] as i16),
            DataType::U32 => f64::from(u32v),
            DataType::I32 => f64::from(u32v as i32),
            DataType::F32 => f64::from(f32::from_bits(u32v)),
        };
        if !raw.is_finite() {
            return Ok((0.0, true));
        }
        let bad = self.bad_values.iter().any(|b| (b - raw).abs() < 1e-9);
        // 去掉 0.1 之类倍率带来的浮点尾数
        let v = ((raw * self.scale + self.offset) * 1e6).round() / 1e6;
        Ok((v, bad))
    }
}

/// 一条链路的连接。断开后下次读时重连。
pub struct Bus {
    link: Link,
    ctx: Option<Context>,
}

impl Bus {
    pub fn new(link: Link) -> Self {
        Bus { link, ctx: None }
    }

    pub fn link(&self) -> &Link {
        &self.link
    }

    async fn connect(link: Link, timeout: Duration) -> Result<Context, String> {
        match &link {
            Link::Tcp { host, port } => {
                let addr =
                    tokio::time::timeout(timeout, tokio::net::lookup_host((host.as_str(), *port)))
                        .await
                        .map_err(|_| format!("解析 {host} 超时"))?
                        .map_err(|e| format!("解析 {host} 失败：{e}"))?
                        .next()
                        .ok_or_else(|| format!("解析 {host} 没有地址"))?;
                let stream = tokio::time::timeout(timeout, tokio::net::TcpStream::connect(addr))
                    .await
                    .map_err(|_| format!("连接 {addr} 超时"))?
                    .map_err(|e| format!("连接 {addr} 失败：{e}"))?;
                Ok(tcp::attach(stream))
            }
            Link::Rtu {
                path,
                baud,
                parity,
                data_bits,
                stop_bits,
            } => {
                use tokio_serial::SerialPortBuilderExt;
                let builder = tokio_serial::new(path, *baud)
                    .parity(match parity {
                        Parity::None => tokio_serial::Parity::None,
                        Parity::Even => tokio_serial::Parity::Even,
                        Parity::Odd => tokio_serial::Parity::Odd,
                    })
                    .data_bits(match data_bits {
                        5 => tokio_serial::DataBits::Five,
                        6 => tokio_serial::DataBits::Six,
                        7 => tokio_serial::DataBits::Seven,
                        _ => tokio_serial::DataBits::Eight,
                    })
                    .stop_bits(if *stop_bits == 2 {
                        tokio_serial::StopBits::Two
                    } else {
                        tokio_serial::StopBits::One
                    });
                let port = builder
                    .open_native_async()
                    .map_err(|e| format!("打开串口 {path} 失败：{e}"))?;
                Ok(rtu::attach(port))
            }
        }
    }

    /// 读一个点位，返回 (值, 是否故障值)。出错时断开连接，下次重连。
    pub async fn read(&mut self, p: &ModbusPoint) -> Result<(f64, bool), String> {
        if self.ctx.is_none() {
            self.ctx = Some(Self::connect(self.link.clone(), p.timeout).await?);
        }
        let ctx = self.ctx.as_mut().expect("刚连上");
        ctx.set_slave(Slave(p.slave));
        let cnt = p.data_type.words();
        let fut = async {
            match p.function {
                Function::Holding => ctx.read_holding_registers(p.register, cnt).await,
                Function::Input => ctx.read_input_registers(p.register, cnt).await,
            }
        };
        let res = match tokio::time::timeout(p.timeout, fut).await {
            Err(_) => Err("读寄存器超时".to_string()),
            Ok(Err(e)) => Err(format!("通讯错误：{e}")),
            Ok(Ok(Err(code))) => {
                // 仪表回了异常码：链路是好的，不必重连
                return Err(format!("仪表返回异常码 {code:?}（检查地址 / 寄存器）"));
            }
            Ok(Ok(Ok(words))) => return p.decode(&words),
        };
        // 超时或通讯错误：丢掉连接，下次重连（RS485 上一个仪表坏了不影响其他地址）
        if matches!(self.link, Link::Tcp { .. }) {
            self.ctx = None;
        }
        res
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn point(extra: &str) -> ModbusPoint {
        let t: toml::Table =
            toml::from_str(&format!("host = \"h\"\nregister = 0\n{extra}")).unwrap();
        ModbusPoint::from_params(DriverKind::ModbusTcp, &t).unwrap()
    }

    #[test]
    fn decode_types() {
        let p = point("scale = 0.1");
        assert_eq!(p.decode(&[13000]).unwrap(), (1300.0, false));
        assert_eq!(p.decode(&[(-50i16) as u16]).unwrap().0, -5.0);
        let p = point("data_type = \"u32\"\nscale = 0.1");
        assert_eq!(p.decode(&[0x0001, 0x0000]).unwrap().0, 6553.6);
        let p = point("data_type = \"u32\"\nword_order = \"little\"");
        assert_eq!(p.decode(&[0x0000, 0x0001]).unwrap().0, 65536.0);
        let bits = 1234.5f32.to_bits();
        let p = point("data_type = \"f32\"");
        assert_eq!(
            p.decode(&[(bits >> 16) as u16, bits as u16]).unwrap().0,
            1234.5
        );
        let p = point("data_type = \"i32\"");
        assert_eq!(p.decode(&[0xFFFF, 0xFFFE]).unwrap().0, -2.0);
    }

    #[test]
    fn bad_values_and_short_reply() {
        let p = point("scale = 0.1\nbad_values = [32767]");
        assert_eq!(p.decode(&[32767]).unwrap(), (3276.7, true));
        let p = point("data_type = \"f32\"");
        assert!(p.decode(&[1]).is_err());
    }

    #[test]
    fn rtu_link_and_tcp_port() {
        let t: toml::Table =
            toml::from_str("port = \"/dev/ttyUSB0\"\nbaud = 19200\nparity = \"even\"\nregister = 4096\naddress = 3")
                .unwrap();
        let p = ModbusPoint::from_params(DriverKind::ModbusRtu, &t).unwrap();
        assert_eq!(p.slave, 3);
        assert_eq!(p.link.key(), "rtu:///dev/ttyUSB0");
        let t: toml::Table =
            toml::from_str("host = \"10.0.0.1\"\nport = \"502\"\nregister = 1").unwrap();
        assert!(ModbusPoint::from_params(DriverKind::ModbusTcp, &t).is_err());
    }
}
