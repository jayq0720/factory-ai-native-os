//! 守护进程：采集（每条链路一个任务）→ 封批落盘 → 上行（断网就留在缓存，恢复后按序号补传）。
//!
//! ```text
//! 仪表 / PLC ──Modbus──▶ 链路任务 ──读数──▶ 封批任务 ──seq + 落盘──▶ SQLite 缓存
//!                                                                    │
//!          云端回执里的签名命令 ◀── 上行任务（按 seq 顺序，失败退避重试）◀┘
//! ```

use std::collections::{BTreeMap, HashMap, HashSet};
use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use fos_protocol::{
    EdgeCommand, EdgeStatus, PointHealth, Quality, Reading, SourcePath, UplinkBatch,
    PROTOCOL_VERSION,
};
use tokio::sync::{mpsc, watch, Notify};
use tokio::time::{sleep, sleep_until, Instant};

use crate::buffer::Buffer;
use crate::commands::Verifier;
use crate::config::{DataMode, EdgeConfig};
use crate::modbus::{Bus, Link, ModbusPoint};
use crate::uplink::{SendError, Uplink};

pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// 节奏参数。生产环境由配置换算；测试里用毫秒级加快。
#[derive(Debug, Clone)]
pub struct Timing {
    pub flush: Duration,
    pub heartbeat: Duration,
    pub retry_min: Duration,
    pub retry_max: Duration,
    pub prune_every: Duration,
}

impl Timing {
    pub fn from_config(cfg: &EdgeConfig) -> Self {
        Timing {
            flush: Duration::from_secs(cfg.flush_s.into()),
            heartbeat: Duration::from_secs(cfg.heartbeat_s.into()),
            retry_min: Duration::from_secs(2),
            retry_max: Duration::from_secs(300),
            prune_every: Duration::from_secs(600),
        }
    }
}

/// 一个要采的点位。
#[derive(Debug, Clone)]
pub struct Target {
    pub id: String,
    pub unit: String,
    pub source: SourcePath,
    pub interval: Duration,
    pub modbus: ModbusPoint,
}

impl Target {
    pub fn from_config(cfg: &EdgeConfig) -> Result<Vec<Target>, String> {
        cfg.points
            .iter()
            .filter(|p| p.collected())
            .map(|p| {
                Ok(Target {
                    id: p.id.clone(),
                    unit: p.unit.clone(),
                    source: p.source_path(),
                    interval: Duration::from_secs(p.interval_s.into()),
                    modbus: p.modbus()?,
                })
            })
            .collect()
    }
}

/// 各任务共享的状态。
pub struct Shared {
    tenant_id: String,
    edge_id: String,
    mock: bool,
    buffer: Mutex<Buffer>,
    health: Mutex<BTreeMap<String, PointHealth>>,
    /// 云端 ReadNow 要求立即读的点位
    forced: Mutex<HashSet<String>>,
    bus_wake: Notify,
    flush_now: Notify,
    uplink_wake: Notify,
    want_status: Mutex<bool>,
}

impl Shared {
    pub fn new(cfg: &EdgeConfig, buffer: Buffer) -> Arc<Self> {
        Arc::new(Shared {
            tenant_id: cfg.tenant_id.clone(),
            edge_id: cfg.edge_id.clone(),
            mock: cfg.mode == DataMode::Mock,
            buffer: Mutex::new(buffer),
            health: Mutex::new(BTreeMap::new()),
            forced: Mutex::new(HashSet::new()),
            bus_wake: Notify::new(),
            flush_now: Notify::new(),
            uplink_wake: Notify::new(),
            want_status: Mutex::new(false),
        })
    }

    pub fn buffer(&self) -> std::sync::MutexGuard<'_, Buffer> {
        self.buffer.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn health(&self) -> std::sync::MutexGuard<'_, BTreeMap<String, PointHealth>> {
        self.health.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 记录一次读数结果；只在状态变化时打日志，避免仪表坏了刷屏。
    fn record(&self, point: &str, result: Result<(), &str>) {
        let mut h = self.health();
        let e = h.entry(point.to_string()).or_insert_with(|| PointHealth {
            point: point.to_string(),
            last_ok_ms: None,
            last_error: None,
            consecutive_errors: 0,
        });
        match result {
            Ok(()) => {
                if e.consecutive_errors > 0 {
                    tracing::info!(
                        point,
                        "恢复采集（之前连续失败 {} 次）",
                        e.consecutive_errors
                    );
                }
                e.last_ok_ms = Some(now_ms());
                e.consecutive_errors = 0;
            }
            Err(msg) => {
                if e.consecutive_errors == 0 || e.last_error.as_deref() != Some(msg) {
                    tracing::warn!(point, "采集失败：{msg}");
                }
                e.last_error = Some(msg.to_string());
                e.consecutive_errors = e.consecutive_errors.saturating_add(1);
            }
        }
    }

    pub fn status(&self) -> EdgeStatus {
        let st = self.buffer().stats().unwrap_or_default();
        EdgeStatus {
            version: env!("CARGO_PKG_VERSION").into(),
            pending_batches: st.pending_batches,
            pending_readings: st.pending_readings,
            oldest_pending_ms: st.oldest_pending_ms,
            rejected_batches: st.rejected_batches,
            points: self.health().values().cloned().collect(),
        }
    }

    fn take_forced(&self, ids: &[&str]) -> HashSet<String> {
        let mut f = self.forced.lock().unwrap_or_else(|e| e.into_inner());
        let hit: HashSet<String> = ids
            .iter()
            .filter(|i| f.contains(**i))
            .map(|s| s.to_string())
            .collect();
        f.retain(|i| !hit.contains(i));
        hit
    }

    /// 执行一条已验签的命令。全部只读：读一次、报状态、重新排队补传。
    pub fn execute(&self, cmd: &EdgeCommand) {
        match cmd {
            EdgeCommand::ReadNow { points } => {
                tracing::info!("命令：立即读 {:?}", points);
                self.forced
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .extend(points.iter().cloned());
                self.bus_wake.notify_waiters();
            }
            EdgeCommand::ReportStatus => {
                tracing::info!("命令：上报状态");
                *self.want_status.lock().unwrap_or_else(|e| e.into_inner()) = true;
                self.flush_now.notify_one();
            }
            EdgeCommand::Resend { from_seq } => match self.buffer().requeue_from(*from_seq) {
                Ok(n) => {
                    tracing::info!("命令：从序号 {from_seq} 起补传，重新排队 {n} 批");
                    self.uplink_wake.notify_one();
                }
                Err(e) => tracing::error!("补传失败：{e}"),
            },
        }
    }
}

/// 一条链路（一个串口 / 一个 TCP 设备）上的采集循环。点位依次读，各自按间隔。
async fn bus_task(
    link: Link,
    targets: Vec<Target>,
    tx: mpsc::Sender<Reading>,
    shared: Arc<Shared>,
    mut stop: watch::Receiver<bool>,
) {
    tracing::info!("链路 {} 开始采集 {} 个点位", link.key(), targets.len());
    let mut bus = Bus::new(link);
    let ids: Vec<&str> = targets.iter().map(|t| t.id.as_str()).collect();
    let mut due: Vec<Instant> = vec![Instant::now(); targets.len()];
    loop {
        let now = Instant::now();
        let forced = shared.take_forced(&ids);
        for (i, t) in targets.iter().enumerate() {
            if due[i] > now && !forced.contains(&t.id) {
                continue;
            }
            if due[i] <= now {
                // 落后太多（例如链路卡住）就从现在重新计时，不补读
                due[i] = (due[i] + t.interval).max(now);
            }
            let ts_ms = now_ms();
            match bus.read(&t.modbus).await {
                Ok((value, bad)) => {
                    shared.record(
                        &t.id,
                        if bad {
                            Err("仪表返回故障值（断偶 / 超量程？）")
                        } else {
                            Ok(())
                        },
                    );
                    let r = Reading {
                        point: t.id.clone(),
                        ts_ms,
                        value,
                        unit: t.unit.clone(),
                        source: t.source,
                        quality: if bad { Quality::Bad } else { Quality::Good },
                        confidence: None,
                        evidence: None,
                        mock: shared.mock,
                    };
                    if tx.send(r).await.is_err() {
                        return;
                    }
                }
                Err(e) => shared.record(&t.id, Err(&e)),
            }
            if *stop.borrow() {
                return;
            }
        }
        let next = due
            .iter()
            .min()
            .copied()
            .unwrap_or_else(|| Instant::now() + Duration::from_secs(60));
        tokio::select! {
            _ = sleep_until(next) => {}
            _ = shared.bus_wake.notified() => {}
            _ = stop.changed() => return,
        }
    }
}

/// 封批：每 `flush` 把攒下的读数封成一批落盘；没有读数时每 `heartbeat` 封一个空批次当心跳。
async fn batch_task(
    mut rx: mpsc::Receiver<Reading>,
    shared: Arc<Shared>,
    timing: Timing,
    max_batch: usize,
    mut stop: watch::Receiver<bool>,
) {
    let mut pending: Vec<Reading> = Vec::new();
    let mut last = Instant::now();
    let mut tick = tokio::time::interval(timing.flush);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let flush = |pending: &mut Vec<Reading>, last: &mut Instant| {
        let r = shared.buffer().enqueue(pending, now_ms());
        match r {
            Ok(_) => {
                pending.clear();
                *last = Instant::now();
                shared.uplink_wake.notify_one();
            }
            Err(e) => {
                tracing::error!("写缓存失败（磁盘满？）：{e}；读数先留在内存");
                // 内存里最多留 10 批，再多就丢最早的，保住进程
                let cap = max_batch * 10;
                if pending.len() > cap {
                    let drop = pending.len() - cap;
                    tracing::error!("内存积压超限，丢弃最早的 {drop} 条读数");
                    pending.drain(..drop);
                }
            }
        }
    };
    loop {
        tokio::select! {
            r = rx.recv() => match r {
                Some(r) => {
                    pending.push(r);
                    if pending.len() >= max_batch {
                        flush(&mut pending, &mut last);
                    }
                }
                None => break,
            },
            _ = tick.tick() => {
                if !pending.is_empty() || last.elapsed() >= timing.heartbeat {
                    flush(&mut pending, &mut last);
                }
            }
            _ = shared.flush_now.notified() => flush(&mut pending, &mut last),
            _ = stop.changed() => break,
        }
    }
    // 退出前把收到的读数都落盘
    while let Ok(r) = rx.try_recv() {
        pending.push(r);
    }
    if !pending.is_empty() {
        flush(&mut pending, &mut last);
    }
}

/// 上行：按序号顺序发；失败退避重试；回执里的命令验签后执行。
async fn uplink_task<U: Uplink>(
    uplink: U,
    shared: Arc<Shared>,
    mut verifier: Verifier,
    timing: Timing,
    buffer_days: u32,
    mut stop: watch::Receiver<bool>,
) {
    let mut backoff = timing.retry_min;
    let mut last_prune = Instant::now() - timing.prune_every;
    let mut failing_since: Option<i64> = None;
    let mut logged_rejects: HashMap<String, String> = HashMap::new();
    loop {
        if *stop.borrow() {
            return;
        }
        if last_prune.elapsed() >= timing.prune_every {
            last_prune = Instant::now();
            match shared.buffer().prune(now_ms(), buffer_days) {
                Ok(0) => {}
                Ok(n) => tracing::error!("断网超过 {buffer_days} 天，丢弃最早的 {n} 条未上行读数"),
                Err(e) => tracing::error!("清理缓存失败：{e}"),
            }
        }
        let next = shared.buffer().next_pending();
        let p = match next {
            Ok(Some(p)) => p,
            Ok(None) => {
                tokio::select! {
                    _ = shared.uplink_wake.notified() => {}
                    _ = sleep(Duration::from_secs(5)) => {}
                    _ = stop.changed() => return,
                }
                continue;
            }
            Err(e) => {
                tracing::error!("读缓存失败：{e}");
                tokio::select! {
                    _ = sleep(timing.retry_max) => {}
                    _ = stop.changed() => return,
                }
                continue;
            }
        };
        let now = now_ms();
        let status = {
            let mut w = shared.want_status.lock().unwrap_or_else(|e| e.into_inner());
            std::mem::take(&mut *w)
        };
        let batch = UplinkBatch {
            protocol: PROTOCOL_VERSION,
            tenant_id: shared.tenant_id.clone(),
            edge_id: shared.edge_id.clone(),
            seq: p.seq,
            // 生成后超过 3 个封批周期才发出去，说明中间断过网
            backfill: now - p.created_ms > 3 * timing.flush.as_millis() as i64,
            readings: p.readings,
            status: status.then(|| shared.status()),
        };
        let res = tokio::select! {
            r = uplink.send(&batch) => r,
            _ = stop.changed() => return, // 没发完的留在缓存，下次启动重发，云端按序号去重
        };
        match res {
            Ok(ack) => {
                if let Err(e) = shared.buffer().mark_sent(p.seq, now_ms()) {
                    tracing::error!("标记已发失败：{e}");
                }
                if let Some(since) = failing_since.take() {
                    tracing::info!("上行恢复（中断约 {} 秒）", (now_ms() - since) / 1000);
                }
                backoff = timing.retry_min;
                for (point, why) in &ack.rejected {
                    if logged_rejects.get(point) != Some(why) {
                        tracing::warn!(point, "云端拒收：{why}");
                        logged_rejects.insert(point.clone(), why.clone());
                    }
                }
                for c in &ack.commands {
                    match verifier.verify(c, now_ms()) {
                        Ok(cmd) => shared.execute(cmd),
                        Err(e) => tracing::warn!(command_id = %c.command_id, "拒绝命令：{e}"),
                    }
                }
            }
            Err(SendError::Reject(msg)) => {
                tracing::error!(
                    seq = p.seq,
                    "云端拒收整批，暂存待查（fos-edge resend 可重排）：{msg}"
                );
                if let Err(e) = shared.buffer().mark_rejected(p.seq, now_ms(), &msg) {
                    tracing::error!("标记被拒失败：{e}");
                }
            }
            Err(SendError::Retry(msg)) => {
                if failing_since.is_none() {
                    failing_since = Some(now_ms());
                    tracing::warn!("上行失败，数据留在本地缓存，稍后重试：{msg}");
                } else {
                    tracing::debug!("上行仍失败：{msg}");
                }
                if status {
                    *shared.want_status.lock().unwrap_or_else(|e| e.into_inner()) = true;
                }
                tokio::select! {
                    _ = sleep(backoff) => {}
                    _ = stop.changed() => return,
                }
                backoff = (backoff * 2).min(timing.retry_max);
            }
        }
    }
}

/// 运行守护进程直到 `shutdown` 完成。
pub async fn run<U: Uplink + 'static>(
    cfg: &EdgeConfig,
    shared: Arc<Shared>,
    uplink: U,
    verifier: Verifier,
    timing: Timing,
    shutdown: impl Future<Output = ()>,
) -> Result<(), String> {
    let targets = Target::from_config(cfg)?;
    let (stop_tx, stop_rx) = watch::channel(false);
    let (tx, rx) = mpsc::channel::<Reading>(10_000);

    let mut by_link: BTreeMap<String, (Link, Vec<Target>)> = BTreeMap::new();
    for t in targets {
        by_link
            .entry(t.modbus.link.key())
            .or_insert_with(|| (t.modbus.link.clone(), Vec::new()))
            .1
            .push(t);
    }
    let mut tasks = Vec::new();
    for (_, (link, ts)) in by_link {
        tasks.push(tokio::spawn(bus_task(
            link,
            ts,
            tx.clone(),
            shared.clone(),
            stop_rx.clone(),
        )));
    }
    drop(tx);
    let batcher = tokio::spawn(batch_task(
        rx,
        shared.clone(),
        timing.clone(),
        cfg.max_batch,
        stop_rx.clone(),
    ));
    let up = tokio::spawn(uplink_task(
        uplink,
        shared.clone(),
        verifier,
        timing,
        cfg.buffer_days,
        stop_rx,
    ));

    shutdown.await;
    tracing::info!("收到退出信号，把内存里的读数落盘后退出");
    let _ = stop_tx.send(true);
    for t in tasks {
        let _ = t.await;
    }
    let _ = batcher.await;
    let _ = up.await;
    Ok(())
}
