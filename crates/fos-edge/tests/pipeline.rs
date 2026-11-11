//! 端到端：仪表模拟器 → Modbus 采集 → 封批落盘 → 上行（中途断网）→ 补传。

use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use ed25519_dalek::{Signer, SigningKey};
use fos_edge::buffer::Buffer;
use fos_edge::commands::Verifier;
use fos_edge::config::EdgeConfig;
use fos_edge::daemon::{self, now_ms, Shared, Timing};
use fos_edge::modbus::{Bus, ModbusPoint};
use fos_edge::sim::{self, setpoint, SimOptions};
use fos_edge::uplink::{SendError, Uplink};
use fos_protocol::{
    DriverKind, EdgeCommand, Quality, SignedCommand, UplinkAck, UplinkBatch, PROTOCOL_VERSION,
};

#[derive(Default)]
struct State {
    online: bool,
    reject_next: bool,
    received: Vec<UplinkBatch>,
    attempts: usize,
    commands: Vec<SignedCommand>,
}

#[derive(Clone, Default)]
struct FakeCloud(Arc<Mutex<State>>);

impl Uplink for FakeCloud {
    async fn send(&self, batch: &UplinkBatch) -> Result<UplinkAck, SendError> {
        let mut s = self.0.lock().unwrap();
        s.attempts += 1;
        if !s.online {
            return Err(SendError::Retry("网络不可达".into()));
        }
        if std::mem::take(&mut s.reject_next) {
            return Err(SendError::Reject("422：格式错误".into()));
        }
        let duplicate = s.received.iter().any(|b| b.seq == batch.seq);
        if !duplicate {
            s.received.push(batch.clone());
        }
        Ok(UplinkAck {
            duplicate,
            seq: batch.seq,
            accepted: batch.readings.len() as u64,
            rejected: Default::default(),
            commands: std::mem::take(&mut s.commands),
        })
    }
}

fn config(port: u16) -> EdgeConfig {
    let key = B64.encode(signer().verifying_key().to_bytes());
    EdgeConfig::parse(&format!(
        r#"
        tenant_id = "luneng"
        edge_id = "edge-test"
        uplink_url = "http://127.0.0.1:8000/v1/edge/uplink"
        mode = "mock"
        command_public_key = "{key}"
        [[point]]
        id = "k.top"
        driver = "modbus-tcp"
        unit = "℃"
        interval_s = 1
        params = {{ host = "127.0.0.1", port = {port}, address = 1, register = 0, scale = 0.1, bad_values = [32767] }}
        [[point]]
        id = "k.bad"
        driver = "modbus-tcp"
        unit = "℃"
        interval_s = 1
        params = {{ host = "127.0.0.1", port = {port}, address = 3, register = 0, scale = 0.1, bad_values = [32767] }}
        [[point]]
        id = "k.kwh"
        driver = "modbus-tcp"
        unit = "kWh"
        interval_s = 1
        params = {{ host = "127.0.0.1", port = {port}, address = 1, register = 2, data_type = "u32", scale = 0.1 }}
        [[point]]
        id = "k.manual"
        driver = "manual"
        unit = "℃"
        interval_s = 7200
        "#
    ))
    .unwrap()
}

fn signer() -> SigningKey {
    SigningKey::from_bytes(&[42u8; 32])
}

fn sign(id: &str, command: EdgeCommand) -> SignedCommand {
    let mut c = SignedCommand {
        protocol: PROTOCOL_VERSION,
        command_id: id.into(),
        tenant_id: "luneng".into(),
        edge_id: "edge-test".into(),
        expires_ms: now_ms() + 60_000,
        command,
        signature: String::new(),
    };
    c.signature = B64.encode(signer().sign(&c.signing_bytes()).to_bytes());
    c
}

fn fast() -> Timing {
    Timing {
        flush: Duration::from_millis(200),
        heartbeat: Duration::from_millis(400),
        retry_min: Duration::from_millis(50),
        retry_max: Duration::from_millis(200),
        prune_every: Duration::from_secs(60),
    }
}

async fn start_sim(fault_unit: Option<u8>) -> u16 {
    let opts = SimOptions {
        speed: 0.0, // 时间不走，读数可预期
        start_h: 14.0,
        fault_unit,
    };
    let (addr, _h) = sim::serve("127.0.0.1:0".parse().unwrap(), opts)
        .await
        .unwrap();
    addr.port()
}

#[tokio::test]
async fn modbus_driver_reads_simulator() {
    let port = start_sim(Some(3)).await;
    let t: toml::Table = toml::from_str(&format!(
        "host = \"127.0.0.1\"\nport = {port}\naddress = 1\nregister = 0\nscale = 0.1\nbad_values = [32767]"
    ))
    .unwrap();
    let p = ModbusPoint::from_params(DriverKind::ModbusTcp, &t).unwrap();
    let mut bus = Bus::new(p.link.clone());
    let (v, bad) = bus.read(&p).await.unwrap();
    assert!((v - setpoint(14.0)).abs() <= 2.0, "{v}");
    assert!(!bad);

    let mut fault = p.clone();
    fault.slave = 3;
    assert!(bus.read(&fault).await.unwrap().1, "断偶值标为故障");

    let mut missing = p.clone();
    missing.register = 10;
    let e = bus.read(&missing).await.unwrap_err();
    assert!(e.contains("异常码"), "{e}");
    // 异常码之后链路仍可用
    assert!(bus.read(&p).await.is_ok());

    // 连不上的设备：报错而不是卡住
    let t: toml::Table =
        toml::from_str("host = \"127.0.0.1\"\nport = 1\nregister = 0\ntimeout_ms = 300").unwrap();
    let dead = ModbusPoint::from_params(DriverKind::ModbusTcp, &t).unwrap();
    assert!(Bus::new(dead.link.clone()).read(&dead).await.is_err());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn offline_buffer_then_backfill_without_duplicates() {
    let port = start_sim(Some(3)).await;
    let cfg = config(port);
    let cloud = FakeCloud::default();
    let shared = Shared::new(
        &cfg,
        Buffer::in_memory("luneng", "edge-test", now_ms()).unwrap(),
    );
    let verifier = Verifier::new(
        Some(
            fos_edge::commands::parse_public_key(cfg.command_public_key.as_deref().unwrap())
                .unwrap(),
        ),
        "luneng",
        "edge-test",
    );
    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let run = {
        let shared = shared.clone();
        let cloud = cloud.clone();
        tokio::spawn(async move {
            daemon::run(&cfg, shared, cloud, verifier, fast(), async {
                let _ = stop_rx.await;
            })
            .await
        })
    };

    // 1) 断网 2.5 秒：读数都留在本地
    tokio::time::sleep(Duration::from_millis(2500)).await;
    {
        let s = cloud.0.lock().unwrap();
        assert!(s.received.is_empty());
        assert!(s.attempts >= 2, "断网时在退避重试");
    }
    let st = shared.buffer().stats().unwrap();
    assert!(st.pending_batches >= 3, "{st:?}");
    assert!(st.pending_readings >= 6, "{st:?}");

    // 2) 恢复网络，顺带下发一条“上报状态”命令和一条伪造命令；第一批故意被判格式错误
    {
        let mut s = cloud.0.lock().unwrap();
        s.online = true;
        s.reject_next = true;
        let mut forged = sign("forged", EdgeCommand::Resend { from_seq: 0 });
        forged.signature = B64.encode([0u8; 64]);
        s.commands = vec![sign("c1", EdgeCommand::ReportStatus), forged];
    }
    tokio::time::sleep(Duration::from_millis(2000)).await;
    let _ = stop_tx.send(());
    run.await.unwrap().unwrap();

    let received = cloud.0.lock().unwrap().received.clone();
    let seqs: Vec<u64> = received.iter().map(|b| b.seq).collect();
    assert!(
        seqs.windows(2).all(|w| w[0] < w[1]),
        "按序号顺序补传：{seqs:?}"
    );
    assert!(
        received.iter().any(|b| b.backfill),
        "断网期间的批次标为补传"
    );
    let readings: Vec<_> = received.iter().flat_map(|b| b.readings.iter()).collect();
    assert!(readings.iter().all(|r| r.mock), "模拟模式每条读数带 mock");
    assert!(
        readings.iter().all(|r| r.point != "k.manual"),
        "抄表点位不由网关采"
    );
    assert!(readings
        .iter()
        .any(|r| r.point == "k.bad" && r.quality == Quality::Bad));
    let top: Vec<_> = readings.iter().filter(|r| r.point == "k.top").collect();
    assert!(top
        .iter()
        .all(|r| r.quality == Quality::Good && (r.value - 1300.0).abs() <= 2.0));
    assert!(readings
        .iter()
        .any(|r| r.point == "k.kwh" && r.unit == "kWh" && r.value > 52_000.0));
    let status: Vec<_> = received.iter().filter_map(|b| b.status.as_ref()).collect();
    assert_eq!(
        status.len(),
        1,
        "验签通过的 ReportStatus 只执行一次，伪造命令被拒"
    );
    assert!(status[0]
        .points
        .iter()
        .any(|p| p.point == "k.bad" && p.consecutive_errors > 0));

    let st = shared.buffer().stats().unwrap();
    assert_eq!(st.pending_batches, 0, "恢复后积压全部发完：{st:?}");
    assert_eq!(st.rejected_batches, 1, "被拒的那批暂存待查，不阻塞后面");

    // 3) 本地重排补传：已发的批次再发一次，云端判重复，不会多出数据
    let total = cloud.0.lock().unwrap().received.len();
    let first = seqs[0];
    assert!(shared.buffer().requeue_from(first).unwrap() > 0);
    let shared2 = shared.clone();
    let cloud2 = cloud.clone();
    let cfg = config(port);
    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let verifier = Verifier::new(None, "luneng", "edge-test");
    let run = tokio::spawn(async move {
        daemon::run(&cfg, shared2, cloud2, verifier, fast(), async {
            let _ = stop_rx.await;
        })
        .await
    });
    tokio::time::sleep(Duration::from_millis(800)).await;
    let _ = stop_tx.send(());
    run.await.unwrap().unwrap();
    let s = cloud.0.lock().unwrap();
    let mut all: Vec<u64> = s.received.iter().map(|b| b.seq).collect();
    let n = all.len();
    all.dedup();
    assert_eq!(all.len(), n, "没有重复序号");
    assert!(n >= total);
}
