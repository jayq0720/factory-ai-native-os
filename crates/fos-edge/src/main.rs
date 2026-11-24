//! fos-edge 命令行。
//!
//! ```text
//! fos-edge check <edge.toml>                 校验配置（兼容旧写法 --check）
//! fos-edge doctor <edge.toml>                上线前一键自检（配置/API Key 环境变量/公钥/本地缓存）
//! fos-edge run <edge.toml>                   启动采集 + 缓存 + 上行
//! fos-edge status <edge.toml> [--json]       看本地缓存积压、被拒批次（--json 输出机器可读 JSON）
//! fos-edge resend <edge.toml> --from <seq>   把某个序号起的批次重新排队上行
//! fos-edge simulate [--listen 127.0.0.1:5020] [--speed 60] [--start-h 0] [--fault-unit N]
//!                                            起一个 Modbus TCP 仪表模拟器
//! ```

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use fos_edge::buffer::{Buffer, Stats};
use fos_edge::commands::{parse_public_key, Verifier};
use fos_edge::config::{DataMode, EdgeConfig};
use fos_edge::daemon::{self, now_ms, Shared, Timing};
use fos_edge::sim::{self, SimOptions};
use fos_edge::uplink::HttpUplink;
use serde::Serialize;

const USAGE: &str = "用法：
  fos-edge check <edge.toml>
  fos-edge doctor <edge.toml>
  fos-edge run <edge.toml>
  fos-edge status <edge.toml> [--json]
  fos-edge resend <edge.toml> --from <seq>
  fos-edge simulate [--listen 127.0.0.1:5020] [--speed 60] [--start-h 0] [--fault-unit N]";

fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("FOS_EDGE_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_target(false)
        .init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let r = match args.as_slice() {
        ["check" | "--check", path] => check(path),
        ["doctor", path] => doctor(path),
        ["run", path] => block(run(path)),
        ["status", path] => status(path, false),
        ["status", path, "--json"] => status(path, true),
        ["resend", path, "--from", seq] => resend(path, seq),
        ["simulate", rest @ ..] => block(simulate(rest)),
        _ => {
            eprintln!("fos-edge {}\n{USAGE}", env!("CARGO_PKG_VERSION"));
            return ExitCode::from(2);
        }
    };
    match r {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{e}");
            ExitCode::FAILURE
        }
    }
}

fn block(f: impl std::future::Future<Output = Result<(), String>>) -> Result<(), String> {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .map_err(|e| format!("启动运行时失败：{e}"))?
        .block_on(f)
}

fn load(path: &str) -> Result<EdgeConfig, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("读取 {path} 失败：{e}"))?;
    EdgeConfig::parse(&text).map_err(|e| e.to_string())
}

/// 缓存文件的相对路径按配置文件所在目录解析，和从哪里启动无关。
fn buffer_path(cfg_path: &str, cfg: &EdgeConfig) -> PathBuf {
    let p = Path::new(&cfg.buffer_path);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        Path::new(cfg_path)
            .parent()
            .unwrap_or(Path::new("."))
            .join(p)
    }
}

fn open_buffer(cfg_path: &str, cfg: &EdgeConfig) -> Result<Buffer, String> {
    let p = buffer_path(cfg_path, cfg);
    Buffer::open(&p, &cfg.tenant_id, &cfg.edge_id, now_ms())
        .map_err(|e| format!("{}：{e}", p.display()))
}

fn check(path: &str) -> Result<(), String> {
    let c = load(path)?;
    println!(
        "配置有效：tenant={} edge={} 点位 {} 个，缓存 {} 天（协议 v{}）{}",
        c.tenant_id,
        c.edge_id,
        c.points.len(),
        c.buffer_days,
        fos_protocol::PROTOCOL_VERSION,
        if c.mode == DataMode::Mock {
            "，模拟数据"
        } else {
            ""
        }
    );
    for p in &c.points {
        let how = if p.collected() {
            p.modbus().map(|m| m.link.key()).unwrap_or_default()
        } else if p.driver == fos_protocol::DriverKind::Manual {
            "云端按间隔提醒抄表".into()
        } else {
            "v0.1 暂无此驱动，运行时跳过".into()
        };
        println!(
            "  {:<24} {:?} / {:?} / {} / 每 {} 秒  {}",
            p.id,
            p.driver,
            p.source_path(),
            p.unit,
            p.interval_s,
            how
        );
    }
    if c.command_public_key.is_none() {
        println!("  （未配置 command_public_key：拒绝云端一切命令）");
    }
    Ok(())
}

async fn run(path: &str) -> Result<(), String> {
    let cfg = load(path)?;
    let key = std::env::var(&cfg.api_key_env).map_err(|_| {
        format!(
            "环境变量 {} 未设置（上行用的 API Key，不写进配置文件）",
            cfg.api_key_env
        )
    })?;
    let buffer = open_buffer(path, &cfg)?;
    let st = buffer.stats().map_err(|e| e.to_string())?;
    tracing::info!(
        "fos-edge {} 启动：tenant={} edge={} 上行 {}，本地积压 {} 批 / {} 条{}",
        env!("CARGO_PKG_VERSION"),
        cfg.tenant_id,
        cfg.edge_id,
        cfg.uplink_url,
        st.pending_batches,
        st.pending_readings,
        if cfg.mode == DataMode::Mock {
            "（模拟数据）"
        } else {
            ""
        }
    );
    for p in cfg.skipped() {
        tracing::warn!(point = %p.id, "v0.1 暂无 {:?} 驱动，跳过", p.driver);
    }
    let verifier = Verifier::new(
        cfg.command_public_key
            .as_deref()
            .map(parse_public_key)
            .transpose()?,
        &cfg.tenant_id,
        &cfg.edge_id,
    );
    let uplink = HttpUplink::new(&cfg.uplink_url, key)?;
    let shared = Shared::new(&cfg, buffer);
    let timing = Timing::from_config(&cfg);
    daemon::run(&cfg, shared, uplink, verifier, timing, shutdown_signal()).await
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        match signal(SignalKind::terminate()) {
            Ok(mut term) => {
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {}
                    _ = term.recv() => {}
                }
            }
            Err(_) => {
                let _ = tokio::signal::ctrl_c().await;
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

fn fmt_ms(ms: i64) -> String {
    let age = (now_ms() - ms) / 1000;
    format!("{} 分钟前", age / 60)
}

#[derive(Debug, Serialize)]
struct StatusJson {
    version: &'static str,
    tenant_id: String,
    edge_id: String,
    buffer_path: String,
    buffer_bytes: u64,
    pending_batches: u64,
    pending_readings: u64,
    oldest_pending_age_s: Option<i64>,
    sent_batches: u64,
    rejected_batches: u64,
    next_seq: u64,
    rejected_samples: Vec<(u64, String)>,
}

fn build_status_json(
    cfg: &EdgeConfig,
    buf_path: &Path,
    buffer_bytes: u64,
    st: &Stats,
    rejected_samples: Vec<(u64, String)>,
    current_ms: i64,
) -> StatusJson {
    let oldest_pending_age_s = st
        .oldest_pending_ms
        .map(|ms| ((current_ms - ms) / 1000).max(0));
    StatusJson {
        version: env!("CARGO_PKG_VERSION"),
        tenant_id: cfg.tenant_id.clone(),
        edge_id: cfg.edge_id.clone(),
        buffer_path: buf_path.display().to_string(),
        buffer_bytes,
        pending_batches: st.pending_batches,
        pending_readings: st.pending_readings,
        oldest_pending_age_s,
        sent_batches: st.sent_batches,
        rejected_batches: st.rejected_batches,
        next_seq: st.next_seq,
        rejected_samples,
    }
}

fn status(path: &str, as_json: bool) -> Result<(), String> {
    let cfg = load(path)?;
    let bp = buffer_path(path, &cfg);
    let b = open_buffer(path, &cfg)?;
    let st = b.stats().map_err(|e| e.to_string())?;
    let rejected = b.rejected(10).map_err(|e| e.to_string())?;
    if as_json {
        let bytes = std::fs::metadata(&bp).map(|m| m.len()).unwrap_or(0);
        let payload = build_status_json(&cfg, &bp, bytes, &st, rejected, now_ms());
        let txt = serde_json::to_string_pretty(&payload).map_err(|e| e.to_string())?;
        println!("{txt}");
        return Ok(());
    }
    println!("缓存文件：{}", bp.display());
    println!(
        "待上行：{} 批 / {} 条读数",
        st.pending_batches, st.pending_readings
    );
    if let Some(o) = st.oldest_pending_ms {
        println!("最早一批积压：{}", fmt_ms(o));
    }
    println!(
        "已上行（保留 1 天）：{} 批；下一个序号 {}",
        st.sent_batches, st.next_seq
    );
    println!("被云端拒收：{} 批", st.rejected_batches);
    for (seq, note) in rejected {
        println!("  seq {seq}：{note}");
    }
    Ok(())
}

fn doctor(path: &str) -> Result<(), String> {
    let cfg = load(path)?;
    let mut failed = false;
    println!(
        "[OK]   配置解析通过：tenant={} edge={} 点位 {} 个",
        cfg.tenant_id,
        cfg.edge_id,
        cfg.points.len()
    );
    match std::env::var(&cfg.api_key_env) {
        Ok(v) if !v.trim().is_empty() => {
            println!("[OK]   上行密钥环境变量 {} 已设置", cfg.api_key_env);
        }
        _ => {
            println!("[FAIL] 环境变量 {} 未设置或为空", cfg.api_key_env);
            failed = true;
        }
    }
    match cfg.command_public_key.as_deref() {
        Some(pk) => match parse_public_key(pk) {
            Ok(_) => println!("[OK]   云端命令 Ed25519 公钥格式合法"),
            Err(e) => {
                println!("[FAIL] 云端命令公钥无效：{e}");
                failed = true;
            }
        },
        None => println!("[WARN] 未配置 command_public_key（将拒绝一切云端只读命令）"),
    }
    match open_buffer(path, &cfg).and_then(|b| b.stats().map_err(|e| e.to_string())) {
        Ok(st) => println!(
            "[OK]   本地缓存可读写：待上行 {} 批，next_seq={}",
            st.pending_batches, st.next_seq
        ),
        Err(e) => {
            println!("[FAIL] 本地缓存打开失败：{e}");
            failed = true;
        }
    }
    if failed {
        Err("fos-edge doctor 发现必须修复的上线阻断项".into())
    } else {
        Ok(())
    }
}

fn resend(path: &str, seq: &str) -> Result<(), String> {
    let from: u64 = seq.parse().map_err(|_| format!("序号无效：{seq}"))?;
    let cfg = load(path)?;
    let n = open_buffer(path, &cfg)?
        .requeue_from(from)
        .map_err(|e| e.to_string())?;
    println!(
        "已把序号 {from} 起的 {n} 批重新排队；运行中的 fos-edge 会在下一轮发出（云端按序号去重）"
    );
    Ok(())
}

async fn simulate(rest: &[&str]) -> Result<(), String> {
    let mut listen: SocketAddr = "127.0.0.1:5020".parse().expect("常量地址");
    let mut opts = SimOptions::default();
    let mut it = rest.iter();
    while let Some(flag) = it.next() {
        let v = it
            .next()
            .ok_or_else(|| format!("{flag} 缺少取值\n{USAGE}"))?;
        let bad = || format!("{flag} 的取值无效：{v}");
        match *flag {
            "--listen" => listen = v.parse().map_err(|_| bad())?,
            "--speed" => opts.speed = v.parse().map_err(|_| bad())?,
            "--start-h" => opts.start_h = v.parse().map_err(|_| bad())?,
            "--fault-unit" => opts.fault_unit = Some(v.parse().map_err(|_| bad())?),
            _ => return Err(format!("未知参数 {flag}\n{USAGE}")),
        }
    }
    let speed = opts.speed;
    let (addr, handle) = sim::serve(listen, opts)
        .await
        .map_err(|e| format!("监听 {listen} 失败：{e}"))?;
    tracing::info!(
        "仪表模拟器（Modbus TCP）在 {addr}，{speed} 倍速；从站 1 = 窑顶，2 = 窑底；寄存器 0 温度×10，1 设定×10，2–3 电量 kWh×10"
    );
    tokio::select! {
        _ = handle => Err("模拟器意外退出".into()),
        _ = shutdown_signal() => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_json_computes_age_and_serializes() {
        let raw = include_str!("../edge.example.toml");
        let cfg = EdgeConfig::parse(raw).expect("example config valid");
        let st = Stats {
            pending_batches: 3,
            pending_readings: 42,
            oldest_pending_ms: Some(1_000_000),
            rejected_batches: 1,
            sent_batches: 10,
            next_seq: 99,
        };
        let payload = build_status_json(
            &cfg,
            Path::new("/tmp/edge.db"),
            4096,
            &st,
            vec![(12, "bad unit".into())],
            1_125_000,
        );
        let val = serde_json::to_value(&payload).expect("serialize status json");
        assert_eq!(val["tenant_id"], cfg.tenant_id);
        assert_eq!(val["edge_id"], cfg.edge_id);
        assert_eq!(val["pending_batches"], 3);
        assert_eq!(val["pending_readings"], 42);
        assert_eq!(val["oldest_pending_age_s"], 125);
        assert_eq!(val["buffer_bytes"], 4096);
        assert_eq!(val["rejected_batches"], 1);
    }

    #[test]
    fn doctor_fails_when_api_key_env_missing() {
        let dir = std::env::temp_dir().join(format!("fos_edge_doc_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let cfg_path = dir.join("edge.toml");
        let raw = include_str!("../edge.example.toml");
        std::fs::write(&cfg_path, raw).expect("write temp edge.toml");
        let res = doctor(cfg_path.to_str().expect("utf8 path"));
        let _ = std::fs::remove_dir_all(&dir);
        if std::env::var("FOS_EDGE_API_KEY").is_err() {
            assert!(res.is_err());
        }
    }
}
