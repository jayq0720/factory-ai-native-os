//! 断网缓存（SQLite）。读数先封批、编号、落盘，再上行；上行成功才标记已发。
//!
//! - 序号 `seq` 在落盘时分配，补传时不变，云端按 (edge_id, seq) 去重，所以重发不会重复入库；
//! - 新建缓存文件时序号从当前 Unix 毫秒起步：缓存文件丢了、换了工控机，新序号也一定比旧的大，
//!   不会被云端当成重复批次悄悄丢掉；
//! - 未上行的批次保留 `buffer_days` 天（默认 7 天），已上行的保留 1 天备查；
//! - 被云端判为格式错误的批次标 `rejected`，不阻塞后面的数据，也不删除，可用 `fos-edge resend` 重新排队。

use std::path::Path;

use fos_protocol::Reading;
use rusqlite::{params, Connection, OptionalExtension};

const DAY_MS: i64 = 86_400_000;

#[derive(Debug, thiserror::Error)]
pub enum BufferError {
    #[error("缓存文件错误：{0}")]
    Sql(#[from] rusqlite::Error),
    #[error("缓存数据损坏：{0}")]
    Json(#[from] serde_json::Error),
    #[error("{0}")]
    Mismatch(String),
}

pub struct Buffer {
    conn: Connection,
}

/// 一个待上行的批次。
#[derive(Debug, Clone)]
pub struct Pending {
    pub seq: u64,
    pub created_ms: i64,
    pub readings: Vec<Reading>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Stats {
    pub pending_batches: u64,
    pub pending_readings: u64,
    pub oldest_pending_ms: Option<i64>,
    pub rejected_batches: u64,
    pub sent_batches: u64,
    pub next_seq: u64,
}

impl Buffer {
    /// 打开（或新建）缓存文件。文件属于哪个租户 / 网关记在里面，配置改了 edge_id 会拒绝打开。
    pub fn open(
        path: impl AsRef<Path>,
        tenant_id: &str,
        edge_id: &str,
        now_ms: i64,
    ) -> Result<Self, BufferError> {
        let conn = Connection::open(path)?;
        Self::init(conn, tenant_id, edge_id, now_ms)
    }

    pub fn in_memory(tenant_id: &str, edge_id: &str, now_ms: i64) -> Result<Self, BufferError> {
        Self::init(Connection::open_in_memory()?, tenant_id, edge_id, now_ms)
    }

    fn init(
        conn: Connection,
        tenant_id: &str,
        edge_id: &str,
        now_ms: i64,
    ) -> Result<Self, BufferError> {
        conn.pragma_update(None, "journal_mode", "WAL")?;
        // 断电时最多丢最后一个事务，不会损坏文件
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS batches (
                 seq INTEGER PRIMARY KEY,
                 created_ms INTEGER NOT NULL,
                 n INTEGER NOT NULL,
                 readings TEXT NOT NULL,
                 state TEXT NOT NULL DEFAULT 'pending',
                 done_ms INTEGER,
                 note TEXT
             );
             CREATE INDEX IF NOT EXISTS batches_state ON batches (state, seq);",
        )?;
        let b = Buffer { conn };
        for (k, v) in [("tenant_id", tenant_id), ("edge_id", edge_id)] {
            match b.meta(k)? {
                None => b.set_meta(k, v)?,
                Some(old) if old == v => {}
                Some(old) => {
                    return Err(BufferError::Mismatch(format!(
                        "缓存文件属于 {k}={old}，配置是 {v}；换网关请换 buffer_path，旧文件先补传完"
                    )))
                }
            }
        }
        if b.meta("next_seq")?.is_none() {
            b.set_meta("next_seq", &now_ms.max(1).to_string())?;
        }
        Ok(b)
    }

    fn meta(&self, k: &str) -> Result<Option<String>, BufferError> {
        Ok(self
            .conn
            .query_row("SELECT value FROM meta WHERE key = ?1", [k], |r| r.get(0))
            .optional()?)
    }

    fn set_meta(&self, k: &str, v: &str) -> Result<(), BufferError> {
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?1, ?2)
             ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [k, v],
        )?;
        Ok(())
    }

    /// 封一批：分配序号并落盘（同一事务）。空批次也照样封，充当心跳。
    pub fn enqueue(&mut self, readings: &[Reading], now_ms: i64) -> Result<u64, BufferError> {
        let tx = self.conn.transaction()?;
        let seq: u64 = tx
            .query_row("SELECT value FROM meta WHERE key = 'next_seq'", [], |r| {
                r.get::<_, String>(0)
            })?
            .parse()
            .map_err(|_| BufferError::Mismatch("next_seq 损坏".into()))?;
        tx.execute(
            "INSERT INTO batches (seq, created_ms, n, readings) VALUES (?1, ?2, ?3, ?4)",
            params![
                seq as i64,
                now_ms,
                readings.len() as i64,
                serde_json::to_string(readings)?
            ],
        )?;
        tx.execute(
            "UPDATE meta SET value = ?1 WHERE key = 'next_seq'",
            [(seq + 1).to_string()],
        )?;
        tx.commit()?;
        Ok(seq)
    }

    /// 最早的待上行批次。
    pub fn next_pending(&self) -> Result<Option<Pending>, BufferError> {
        let row = self
            .conn
            .query_row(
                "SELECT seq, created_ms, readings FROM batches WHERE state = 'pending' ORDER BY seq LIMIT 1",
                [],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?, r.get::<_, String>(2)?)),
            )
            .optional()?;
        match row {
            None => Ok(None),
            Some((seq, created_ms, j)) => Ok(Some(Pending {
                seq: seq as u64,
                created_ms,
                readings: serde_json::from_str(&j)?,
            })),
        }
    }

    pub fn mark_sent(&self, seq: u64, now_ms: i64) -> Result<(), BufferError> {
        self.conn.execute(
            "UPDATE batches SET state = 'sent', done_ms = ?2, note = NULL WHERE seq = ?1",
            params![seq as i64, now_ms],
        )?;
        Ok(())
    }

    pub fn mark_rejected(&self, seq: u64, now_ms: i64, note: &str) -> Result<(), BufferError> {
        self.conn.execute(
            "UPDATE batches SET state = 'rejected', done_ms = ?2, note = ?3 WHERE seq = ?1",
            params![seq as i64, now_ms, note],
        )?;
        Ok(())
    }

    /// 把 `from_seq` 及之后已发 / 被拒的批次重新排队（云端 Resend 命令或 `fos-edge resend`）。
    /// 已发的批次重发时云端按序号判重，不会重复入库。返回重新排队的批次数。
    pub fn requeue_from(&self, from_seq: u64) -> Result<u64, BufferError> {
        let n = self.conn.execute(
            "UPDATE batches SET state = 'pending', done_ms = NULL WHERE seq >= ?1 AND state != 'pending'",
            [from_seq as i64],
        )?;
        Ok(n as u64)
    }

    /// 清理：已发的留 1 天，被拒的和未发的留 `buffer_days` 天。返回因超期被丢弃的未发读数条数。
    pub fn prune(&self, now_ms: i64, buffer_days: u32) -> Result<u64, BufferError> {
        let keep = now_ms - i64::from(buffer_days) * DAY_MS;
        self.conn.execute(
            "DELETE FROM batches WHERE state = 'sent' AND done_ms < ?1",
            [now_ms - DAY_MS],
        )?;
        self.conn.execute(
            "DELETE FROM batches WHERE state = 'rejected' AND created_ms < ?1",
            [keep],
        )?;
        let lost: i64 = self.conn.query_row(
            "SELECT COALESCE(SUM(n), 0) FROM batches WHERE state = 'pending' AND created_ms < ?1",
            [keep],
            |r| r.get(0),
        )?;
        self.conn.execute(
            "DELETE FROM batches WHERE state = 'pending' AND created_ms < ?1",
            [keep],
        )?;
        Ok(lost as u64)
    }

    pub fn stats(&self) -> Result<Stats, BufferError> {
        let (pb, pr, oldest): (i64, i64, Option<i64>) = self.conn.query_row(
            "SELECT COUNT(*), COALESCE(SUM(n), 0), MIN(created_ms) FROM batches WHERE state = 'pending'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )?;
        let count = |state: &str| -> Result<u64, BufferError> {
            let n: i64 = self.conn.query_row(
                "SELECT COUNT(*) FROM batches WHERE state = ?1",
                [state],
                |r| r.get(0),
            )?;
            Ok(n as u64)
        };
        Ok(Stats {
            pending_batches: pb as u64,
            pending_readings: pr as u64,
            oldest_pending_ms: oldest,
            rejected_batches: count("rejected")?,
            sent_batches: count("sent")?,
            next_seq: self
                .meta("next_seq")?
                .and_then(|s| s.parse().ok())
                .unwrap_or(0),
        })
    }

    /// 最近被拒的批次（序号、原因），给 `fos-edge status` 看。
    pub fn rejected(&self, limit: u32) -> Result<Vec<(u64, String)>, BufferError> {
        let mut st = self.conn.prepare(
            "SELECT seq, COALESCE(note, '') FROM batches WHERE state = 'rejected' ORDER BY seq DESC LIMIT ?1",
        )?;
        let rows = st
            .query_map([limit], |r| Ok((r.get::<_, i64>(0)? as u64, r.get(1)?)))?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use fos_protocol::{Quality, SourcePath};

    pub(crate) fn reading(point: &str, ts_ms: i64, value: f64) -> Reading {
        Reading {
            point: point.into(),
            ts_ms,
            value,
            unit: "℃".into(),
            source: SourcePath::Direct,
            quality: Quality::Good,
            confidence: None,
            evidence: None,
            mock: false,
        }
    }

    #[test]
    fn seq_starts_at_clock_and_survives_reopen() {
        let dir = std::env::temp_dir().join(format!("fos-edge-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("b.db");
        let _ = std::fs::remove_file(&path);
        let mut b = Buffer::open(&path, "t", "e", 1_000).unwrap();
        assert_eq!(b.enqueue(&[reading("a", 1, 1.0)], 1_000).unwrap(), 1_000);
        assert_eq!(b.enqueue(&[], 1_001).unwrap(), 1_001);
        drop(b);
        let mut b = Buffer::open(&path, "t", "e", 99_999).unwrap();
        assert_eq!(
            b.enqueue(&[], 1_002).unwrap(),
            1_002,
            "重开文件序号接着走，不按时钟重置"
        );
        let p = b.next_pending().unwrap().unwrap();
        assert_eq!((p.seq, p.readings.len()), (1_000, 1));
        drop(b);
        let e = Buffer::open(&path, "t", "other", 0).err().unwrap();
        assert!(e.to_string().contains("edge_id"));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn sent_rejected_requeue_and_prune() {
        let mut b = Buffer::in_memory("t", "e", 10).unwrap();
        let s1 = b.enqueue(&[reading("a", 1, 1.0)], 0).unwrap();
        let s2 = b
            .enqueue(&[reading("a", 2, 2.0), reading("b", 2, 3.0)], 0)
            .unwrap();
        let s3 = b.enqueue(&[], DAY_MS * 8).unwrap();
        b.mark_sent(s1, 0).unwrap();
        b.mark_rejected(s2, 0, "422").unwrap();
        let st = b.stats().unwrap();
        assert_eq!(
            (st.pending_batches, st.rejected_batches, st.sent_batches),
            (1, 1, 1)
        );
        assert_eq!(b.next_pending().unwrap().unwrap().seq, s3);
        assert_eq!(b.rejected(5).unwrap(), vec![(s2, "422".to_string())]);

        assert_eq!(b.requeue_from(s2).unwrap(), 1);
        assert_eq!(b.next_pending().unwrap().unwrap().seq, s2);

        // 第 8 天：s1（已发，超过 1 天）删掉；s2 未发且超过 7 天 → 丢弃 2 条读数；s3 保留
        assert_eq!(b.prune(DAY_MS * 8, 7).unwrap(), 2);
        let st = b.stats().unwrap();
        assert_eq!((st.pending_batches, st.sent_batches), (1, 0));
        assert_eq!(st.oldest_pending_ms, Some(DAY_MS * 8));
    }
}
