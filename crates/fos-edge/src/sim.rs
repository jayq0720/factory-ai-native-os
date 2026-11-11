//! 仪表模拟器：一个 Modbus TCP 从站，温度按一条烧成曲线变化，电表累计走字。
//!
//! 现场仪表型号还没调研，先用它把“采集 → 缓存 → 上行 → 云端告警”整条链路跑通；
//! 拿到真实寄存器表后只改 edge.toml，不改代码。模拟数据一律 `mode = "mock"` 上行（规则 4）。
//!
//! 寄存器表（每个从站地址 1–16 都一样，地址 2 模拟窑底，比窑顶低 35℃）：
//!
//! | 寄存器 | 含义 | 类型 |
//! |---|---|---|
//! | 0 | 测量温度 ×10 | i16 |
//! | 1 | 设定温度 ×10 | i16 |
//! | 2–3 | 累计电量 kWh ×10（高字在前） | u32 |
//!
//! 只响应 03 / 04 读功能码；写请求一律回“非法功能”。

use std::future::{ready, Ready};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Instant;

use tokio::net::TcpListener;
use tokio_modbus::server::tcp::{accept_tcp_connection, Server};
use tokio_modbus::server::Service;
use tokio_modbus::{ExceptionCode, Request, Response, SlaveRequest};

/// 故障值：模拟断偶时温度寄存器返回它
pub const FAULT_RAW: u16 = 32767;
/// 模拟的一个窑次（小时）：升温 12 h → 保温 4 h → 降温 20 h
pub const CYCLE_H: f64 = 36.0;

#[derive(Debug, Clone)]
pub struct SimOptions {
    /// 时间倍速：60 表示真实 1 秒 = 模拟 1 分钟
    pub speed: f64,
    /// 从这个从站地址模拟断偶（温度寄存器返回 32767）
    pub fault_unit: Option<u8>,
    /// 从模拟第几小时开始（便于直接看到高温段）
    pub start_h: f64,
}

impl Default for SimOptions {
    fn default() -> Self {
        SimOptions {
            speed: 60.0,
            fault_unit: None,
            start_h: 0.0,
        }
    }
}

/// 烧成曲线目标温度（℃）。
pub fn setpoint(h: f64) -> f64 {
    let h = h.rem_euclid(CYCLE_H);
    if h < 12.0 {
        20.0 + (1300.0 - 20.0) * h / 12.0
    } else if h < 16.0 {
        1300.0
    } else {
        1300.0 - (1300.0 - 60.0) * (h - 16.0) / (CYCLE_H - 16.0)
    }
}

/// 确定性的小噪声（±2℃），避免引入随机数依赖，测试可复现。
fn noise(h: f64, unit: u8) -> f64 {
    let x = ((h * 600.0) as u64).wrapping_mul(6364136223846793005)
        ^ u64::from(unit).wrapping_mul(1442695040888963407);
    let x = x ^ (x >> 29);
    ((x % 4001) as f64 / 1000.0) - 2.0
}

pub struct Kiln {
    started: Instant,
    opts: SimOptions,
}

impl Kiln {
    pub fn new(opts: SimOptions) -> Self {
        Kiln {
            started: Instant::now(),
            opts,
        }
    }

    fn hours(&self) -> f64 {
        self.opts.start_h + self.started.elapsed().as_secs_f64() * self.opts.speed / 3600.0
    }

    /// 某个从站当前的寄存器值。
    pub fn registers(&self, unit: u8, h: f64) -> [u16; 4] {
        let sp = setpoint(h);
        let pv = sp - if unit == 2 { 35.0 } else { 0.0 } + noise(h, unit);
        let temp = if self.opts.fault_unit == Some(unit) {
            FAULT_RAW
        } else {
            ((pv * 10.0).round() as i16) as u16
        };
        let kwh10 = ((52_000.0 + 120.0 * h) * 10.0).round() as u32;
        [
            temp,
            ((sp * 10.0).round() as i16) as u16,
            (kwh10 >> 16) as u16,
            kwh10 as u16,
        ]
    }

    fn read(&self, unit: u8, addr: u16, cnt: u16) -> Result<Vec<u16>, ExceptionCode> {
        if !(1..=16).contains(&unit) {
            return Err(ExceptionCode::GatewayTargetDevice);
        }
        let regs = self.registers(unit, self.hours());
        let start = usize::from(addr);
        let end = start + usize::from(cnt);
        if cnt == 0 || end > regs.len() {
            return Err(ExceptionCode::IllegalDataAddress);
        }
        Ok(regs[start..end].to_vec())
    }
}

#[derive(Clone)]
struct SimService(Arc<Kiln>);

impl Service for SimService {
    type Request = SlaveRequest<'static>;
    type Response = Response;
    type Exception = ExceptionCode;
    type Future = Ready<Result<Response, ExceptionCode>>;

    fn call(&self, req: Self::Request) -> Self::Future {
        let unit = req.slave;
        ready(match req.request {
            Request::ReadHoldingRegisters(a, n) => {
                self.0.read(unit, a, n).map(Response::ReadHoldingRegisters)
            }
            Request::ReadInputRegisters(a, n) => {
                self.0.read(unit, a, n).map(Response::ReadInputRegisters)
            }
            _ => Err(ExceptionCode::IllegalFunction),
        })
    }
}

/// 启动模拟器，一直跑到进程退出。返回实际监听地址（端口 0 时由系统分配）。
pub async fn serve(
    listen: SocketAddr,
    opts: SimOptions,
) -> std::io::Result<(SocketAddr, tokio::task::JoinHandle<()>)> {
    let listener = TcpListener::bind(listen).await?;
    let addr = listener.local_addr()?;
    let kiln = Arc::new(Kiln::new(opts));
    let server = Server::new(listener);
    let handle = tokio::spawn(async move {
        let on_connected = |stream, socket_addr| {
            let kiln = kiln.clone();
            async move {
                accept_tcp_connection(stream, socket_addr, move |_| {
                    Ok(Some(SimService(kiln.clone())))
                })
            }
        };
        let on_error = |e| tracing::warn!("模拟器连接出错：{e}");
        if let Err(e) = server.serve(&on_connected, on_error).await {
            tracing::error!("模拟器退出：{e}");
        }
    });
    Ok((addr, handle))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn curve_shape() {
        assert_eq!(setpoint(0.0), 20.0);
        assert_eq!(setpoint(6.0), 660.0);
        assert_eq!(setpoint(14.0), 1300.0);
        assert!((setpoint(CYCLE_H - 0.0001) - 60.0).abs() < 0.1);
        assert_eq!(setpoint(CYCLE_H), 20.0, "下一窑次重新升温");
    }

    #[test]
    fn registers_and_fault() {
        let k = Kiln::new(SimOptions {
            fault_unit: Some(3),
            ..Default::default()
        });
        let r1 = k.registers(1, 14.0);
        assert!((f64::from(r1[0] as i16) / 10.0 - 1300.0).abs() <= 2.0);
        assert_eq!(r1[1], 13000);
        let r2 = k.registers(2, 14.0);
        assert!((f64::from(r2[0] as i16) / 10.0 - 1265.0).abs() <= 2.0);
        assert_eq!(k.registers(3, 14.0)[0], FAULT_RAW);
        assert!(k.read(1, 3, 2).is_err());
        assert!(k.read(17, 0, 1).is_err());
    }
}
