//! 云端命令验签（方案 7.2）。
//!
//! 命令只能随上行回执下来（厂端不开任何入站端口），协议里只有只读命令。验签通过还要满足：
//! 租户、网关对得上；没过期，且有效期不超过 1 小时（限制重放窗口）；同一个 command_id 只执行一次。

use std::collections::HashMap;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use ed25519_dalek::{Signature, Verifier as _, VerifyingKey};
use fos_protocol::{EdgeCommand, SignedCommand, PROTOCOL_VERSION};

/// 命令最长有效期。云端签发时应远小于它（例如 5 分钟）。
pub const MAX_TTL_MS: i64 = 3_600_000;

pub fn parse_public_key(b64: &str) -> Result<VerifyingKey, String> {
    let bytes = B64
        .decode(b64.trim())
        .map_err(|_| "command_public_key 不是合法的 base64".to_string())?;
    let arr: [u8; 32] = bytes
        .try_into()
        .map_err(|_| "command_public_key 应为 32 字节的 Ed25519 公钥".to_string())?;
    VerifyingKey::from_bytes(&arr)
        .map_err(|_| "command_public_key 不是有效的 Ed25519 公钥".to_string())
}

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum Rejected {
    #[error("未配置验签公钥，拒绝一切命令")]
    NoKey,
    #[error("协议版本不符")]
    Protocol,
    #[error("命令不是发给本网关的")]
    WrongTarget,
    #[error("命令已过期")]
    Expired,
    #[error("命令有效期超过 1 小时")]
    TtlTooLong,
    #[error("命令已执行过（重放）")]
    Replay,
    #[error("签名无效")]
    BadSignature,
}

pub struct Verifier {
    key: Option<VerifyingKey>,
    tenant_id: String,
    edge_id: String,
    /// 已执行的 command_id → 过期时间；过期后清掉（过期命令本来就会被拒）
    seen: HashMap<String, i64>,
}

impl Verifier {
    pub fn new(key: Option<VerifyingKey>, tenant_id: &str, edge_id: &str) -> Self {
        Verifier {
            key,
            tenant_id: tenant_id.into(),
            edge_id: edge_id.into(),
            seen: HashMap::new(),
        }
    }

    pub fn verify<'a>(
        &mut self,
        c: &'a SignedCommand,
        now_ms: i64,
    ) -> Result<&'a EdgeCommand, Rejected> {
        let key = self.key.as_ref().ok_or(Rejected::NoKey)?;
        if c.protocol != PROTOCOL_VERSION {
            return Err(Rejected::Protocol);
        }
        if c.tenant_id != self.tenant_id || c.edge_id != self.edge_id {
            return Err(Rejected::WrongTarget);
        }
        if c.expires_ms <= now_ms {
            return Err(Rejected::Expired);
        }
        if c.expires_ms - now_ms > MAX_TTL_MS {
            return Err(Rejected::TtlTooLong);
        }
        let sig = B64
            .decode(c.signature.trim())
            .ok()
            .and_then(|b| Signature::from_slice(&b).ok())
            .ok_or(Rejected::BadSignature)?;
        key.verify(&c.signing_bytes(), &sig)
            .map_err(|_| Rejected::BadSignature)?;
        self.seen.retain(|_, exp| *exp > now_ms);
        if self.seen.contains_key(&c.command_id) {
            return Err(Rejected::Replay);
        }
        self.seen.insert(c.command_id.clone(), c.expires_ms);
        Ok(&c.command)
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};

    pub(crate) fn signer() -> SigningKey {
        SigningKey::from_bytes(&[7u8; 32])
    }

    pub(crate) fn sign(
        key: &SigningKey,
        id: &str,
        edge: &str,
        expires_ms: i64,
        command: EdgeCommand,
    ) -> SignedCommand {
        let mut c = SignedCommand {
            protocol: PROTOCOL_VERSION,
            command_id: id.into(),
            tenant_id: "luneng".into(),
            edge_id: edge.into(),
            expires_ms,
            command,
            signature: String::new(),
        };
        c.signature = B64.encode(key.sign(&c.signing_bytes()).to_bytes());
        c
    }

    #[test]
    fn accepts_valid_and_rejects_everything_else() {
        let k = signer();
        let pk = B64.encode(k.verifying_key().to_bytes());
        let mut v = Verifier::new(Some(parse_public_key(&pk).unwrap()), "luneng", "e1");
        let ok = sign(&k, "c1", "e1", 60_000, EdgeCommand::ReportStatus);
        assert_eq!(v.verify(&ok, 0), Ok(&EdgeCommand::ReportStatus));
        assert_eq!(v.verify(&ok, 1), Err(Rejected::Replay));
        assert_eq!(v.verify(&ok, 60_000), Err(Rejected::Expired));

        let other = sign(&k, "c2", "e2", 60_000, EdgeCommand::ReportStatus);
        assert_eq!(v.verify(&other, 0), Err(Rejected::WrongTarget));
        let long = sign(&k, "c3", "e1", MAX_TTL_MS + 1, EdgeCommand::ReportStatus);
        assert_eq!(v.verify(&long, 0), Err(Rejected::TtlTooLong));

        // 篡改命令内容（签名不变）
        let mut tampered = sign(&k, "c4", "e1", 60_000, EdgeCommand::Resend { from_seq: 10 });
        tampered.command = EdgeCommand::Resend { from_seq: 0 };
        assert_eq!(v.verify(&tampered, 0), Err(Rejected::BadSignature));

        // 别人的私钥
        let forged = sign(
            &SigningKey::from_bytes(&[9u8; 32]),
            "c5",
            "e1",
            60_000,
            EdgeCommand::ReportStatus,
        );
        assert_eq!(v.verify(&forged, 0), Err(Rejected::BadSignature));

        let mut none = Verifier::new(None, "luneng", "e1");
        assert_eq!(none.verify(&ok, 0), Err(Rejected::NoKey));
    }

    #[test]
    fn bad_public_keys() {
        assert!(parse_public_key("not base64!").is_err());
        assert!(parse_public_key(&B64.encode([1u8; 16])).is_err());
    }
}
