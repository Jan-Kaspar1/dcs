//! The model-fingerprint contract: a deterministic hash naming the plant
//! model a run was assembled from.
//!
//! [`ModelFingerprint`] rides inside checkpoints so a standby can
//! negotiate compatibility explicitly: a checkpoint captured under a
//! different model carries a different fingerprint, and restore rejects
//! it by name before any state applies. The value is supplied by the
//! assembling layer — the executor is model-agnostic — computed as a
//! hash over the model's canonical serialization, so two documents that
//! differ only in key order or whitespace fingerprint identically while
//! any semantic difference fingerprints differently.

use serde::{Deserialize, Serialize};
use std::fmt;

/// A deterministic 64-bit hash identifying one canonical model document.
///
/// The hash is FNV-1a over the document's canonical bytes — a fixed,
/// platform-independent algorithm, so the same model fingerprints
/// identically on every build and host. A fingerprint says nothing on its
/// own; it is meaningful only by comparison — equal means same model,
/// different means a named mismatch.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct ModelFingerprint(pub u64);

impl ModelFingerprint {
    /// FNV-1a over `document`: the stable hash every fingerprint uses.
    pub fn of(document: &[u8]) -> Self {
        let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
        for &byte in document {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
        Self(hash)
    }
}

impl fmt::Display for ModelFingerprint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:016x}", self.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_is_deterministic_and_order_sensitive() {
        assert_eq!(ModelFingerprint::of(b"doc"), ModelFingerprint::of(b"doc"));
        assert_ne!(ModelFingerprint::of(b"doc"), ModelFingerprint::of(b"cod"));
        assert_ne!(ModelFingerprint::of(b""), ModelFingerprint::of(b"\0"));
    }
}
