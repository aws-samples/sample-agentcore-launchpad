//! I4 request authentication — byte-for-byte `codec.engine_signature` on the Python side:
//! `hex(HMAC-SHA256(key, METHOD\nPATH\nTS\nNONCE\nsha256hex(body)))`, ±60 s, nonces
//! rejected on replay for 5 minutes.

use std::collections::HashMap;
use std::sync::Mutex;

use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

pub const SKEW_S: i64 = 60;
pub const NONCE_TTL_S: i64 = 300;

fn mac(key: &[u8], method: &str, path: &str, ts: &str, nonce: &str, body: &[u8]) -> Hmac<Sha256> {
    let body_hash = hex::encode(Sha256::digest(body));
    let message = format!("{}\n{path}\n{ts}\n{nonce}\n{body_hash}", method.to_ascii_uppercase());
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC accepts any key length");
    mac.update(message.as_bytes());
    mac
}

#[cfg_attr(not(test), allow(dead_code))]
pub fn signature(key: &[u8], method: &str, path: &str, ts: &str, nonce: &str, body: &[u8]) -> String {
    hex::encode(mac(key, method, path, ts, nonce, body).finalize().into_bytes())
}

/// N17-obs492 (re-review F9): the nonce cache is per process, so a signed request captured
/// before a restart could be replayed once in the ±`SKEW_S` window after it. For the first
/// `SKEW_S + NONCE_TTL_S` seconds after start the verifier therefore also refuses a timestamp
/// older than the process start (minus this grace for the caller's clock); a legitimate
/// request is never older than "now", so only a replay from before the restart is cut.
pub const START_GRACE_S: i64 = 2;

pub struct Verifier {
    key: Vec<u8>,
    started_s: i64,
    nonces: Mutex<HashMap<String, i64>>,
}

impl Verifier {
    pub fn new(key: Vec<u8>, started_s: i64) -> Self {
        Verifier { key, started_s, nonces: Mutex::new(HashMap::new()) }
    }

    /// Constant-time signature check + timestamp window + one-time nonce (+ the restart rule).
    pub fn check(&self, method: &str, path: &str, ts: &str, nonce: &str, body: &[u8], sig: &str, now_s: i64) -> bool {
        let Ok(t) = ts.parse::<i64>() else { return false };
        if (now_s - t).abs() > SKEW_S || nonce.is_empty() || nonce.len() > 128 {
            return false;
        }
        if now_s - self.started_s <= SKEW_S + NONCE_TTL_S && t < self.started_s - START_GRACE_S {
            return false; // signed before this process started: a replay across the restart
        }
        let Ok(given) = hex::decode(sig) else { return false };
        // constant-time comparison
        if mac(&self.key, method, path, ts, nonce, body).verify_slice(&given).is_err() {
            return false;
        }
        let mut nonces = self.nonces.lock().unwrap();
        nonces.retain(|_, seen| now_s - *seen <= NONCE_TTL_S);
        if nonces.contains_key(nonce) {
            return false;
        }
        nonces.insert(nonce.to_string(), now_s);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signature_matches_the_python_codec_vector() {
        // backend: codec.engine_signature(b"k"*32, "POST", "/v1/decide", "1793022000", "n1", b"{}")
        let sig = signature(&[b'k'; 32], "post", "/v1/decide", "1793022000", "n1", b"{}");
        assert_eq!(sig, include_str!("../tests/python_signature_vector.txt").trim());
    }

    #[test]
    fn a_request_signed_before_the_restart_is_refused_during_the_startup_window() {
        // N17-obs492 / F9: process started at 2000; a request signed at 1990 (captured before
        // the restart, still inside the ±60 s skew) is refused; one signed at 1998 (the
        // caller's clock 2 s behind) and one at 2001 pass; after SKEW_S + NONCE_TTL_S the
        // rule no longer applies (the nonce cache covers the window by then)
        let v = Verifier::new(vec![7; 32], 2000);
        let sig = |ts: &str, n: &str| signature(&[7; 32], "POST", "/v1/decide", ts, n, b"x");
        assert!(!v.check("POST", "/v1/decide", "1990", "a", b"x", &sig("1990", "a"), 2005), "pre-start replay");
        assert!(v.check("POST", "/v1/decide", "1998", "b", b"x", &sig("1998", "b"), 2005), "clock 2 s behind");
        assert!(v.check("POST", "/v1/decide", "2001", "c", b"x", &sig("2001", "c"), 2005));
        let late = 2000 + SKEW_S + NONCE_TTL_S + 1;
        let ts = (late - 10).to_string();
        assert!(v.check("POST", "/v1/decide", &ts, "d", b"x", &sig(&ts, "d"), late), "window over");
    }

    #[test]
    fn replay_window_and_tamper() {
        let v = Verifier::new(vec![7; 32], 0);
        let sig = signature(&[7; 32], "POST", "/v1/decide", "1000", "n", b"x");
        assert!(v.check("POST", "/v1/decide", "1000", "n", b"x", &sig, 1000));
        assert!(!v.check("POST", "/v1/decide", "1000", "n", b"x", &sig, 1000), "replay");
        let sig2 = signature(&[7; 32], "POST", "/v1/decide", "1000", "m", b"x");
        assert!(!v.check("POST", "/v1/decide", "1000", "m", b"y", &sig2, 1000), "body");
        assert!(!v.check("POST", "/v1/respond", "1000", "m", b"x", &sig2, 1000), "path");
        assert!(!v.check("POST", "/v1/decide", "1000", "m", b"x", &sig2, 1100), "skew");
    }
}
