//! 上行：把一批读数 POST 到云端 `/v1/edge/uplink`。
//!
//! 结果分三种：成功（含云端判重复）→ 标记已发；批次本身有问题（400 / 413 / 422）→ 标记被拒、继续发后面的；
//! 其他（断网、超时、5xx、401 / 403 Key 不对）→ 原样留在缓存里，退避后重试，一条都不丢。

use std::time::Duration;

use fos_protocol::{UplinkAck, UplinkBatch};

#[derive(Debug, Clone, PartialEq)]
pub enum SendError {
    /// 稍后重试
    Retry(String),
    /// 云端认为这批数据本身不对，重试也没用
    Reject(String),
}

pub trait Uplink: Send + Sync {
    fn send(
        &self,
        batch: &UplinkBatch,
    ) -> impl std::future::Future<Output = Result<UplinkAck, SendError>> + Send;
}

pub struct HttpUplink {
    client: reqwest::Client,
    url: String,
    api_key: String,
}

impl HttpUplink {
    pub fn new(url: &str, api_key: String) -> Result<Self, String> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .connect_timeout(Duration::from_secs(10))
            .user_agent(concat!("fos-edge/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|e| format!("初始化 HTTP 客户端失败：{e}"))?;
        Ok(HttpUplink {
            client,
            url: url.into(),
            api_key,
        })
    }
}

impl Uplink for HttpUplink {
    async fn send(&self, batch: &UplinkBatch) -> Result<UplinkAck, SendError> {
        let resp = self
            .client
            .post(&self.url)
            .bearer_auth(&self.api_key)
            .json(batch)
            .send()
            .await
            .map_err(|e| SendError::Retry(format!("网络：{e}")))?;
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        if status.is_success() {
            return serde_json::from_str(&body)
                .map_err(|e| SendError::Retry(format!("回执无法解析：{e}")));
        }
        let detail: String = body.chars().take(300).collect();
        match status.as_u16() {
            400 | 413 | 422 => Err(SendError::Reject(format!("{status}：{detail}"))),
            _ => Err(SendError::Retry(format!("{status}：{detail}"))),
        }
    }
}
