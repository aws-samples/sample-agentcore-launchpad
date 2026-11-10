//! One clock for the whole shard: the durable stores stamp events with it and the request
//! context's `system.*` Longs are derived from it, so windows and `local_hour` agree.

use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};

use dogwood_local_engine::{Clock, WallClock};

pub const NANOS: i64 = 1_000_000_000;

#[derive(Clone)]
pub struct SharedClock(Arc<dyn Clock>);

impl SharedClock {
    pub fn wall() -> Self {
        SharedClock(Arc::new(WallClock))
    }

    pub fn manual(start_ns: i64) -> (Self, ManualClock) {
        let handle = ManualClock(Arc::new(AtomicI64::new(start_ns)));
        (SharedClock(Arc::new(handle.clone())), handle)
    }

    pub fn now_nanos_u128(&self) -> u128 {
        self.0.now_nanos().max(0) as u128
    }

    pub fn now_s(&self) -> i64 {
        self.0.now_nanos() / NANOS
    }

    /// `(local_hour 0-23, local_weekday 1=Mon..7=Sun)` at a fixed UTC offset (the display
    /// zone `Asia/Shanghai` has no DST, so a fixed offset is exact).
    pub fn local(&self, utc_offset_s: i64) -> (i64, i64) {
        let t = self.now_s() + utc_offset_s;
        let days = t.div_euclid(86_400);
        let hour = t.rem_euclid(86_400) / 3_600;
        // 1970-01-01 was a Thursday (ISO 4)
        let weekday = (days + 3).rem_euclid(7) + 1;
        (hour, weekday)
    }

    pub fn boxed(&self) -> Box<dyn Clock> {
        Box::new(self.clone())
    }
}

impl Clock for SharedClock {
    fn now_nanos(&self) -> i64 {
        self.0.now_nanos()
    }
}

/// A test clock the caller advances.
#[derive(Clone)]
pub struct ManualClock(Arc<AtomicI64>);

impl ManualClock {
    pub fn advance_s(&self, seconds: i64) {
        self.0.fetch_add(seconds * NANOS, Ordering::SeqCst);
    }
}

impl Clock for ManualClock {
    fn now_nanos(&self) -> i64 {
        self.0.load(Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_hour_and_weekday() {
        // 2026-10-26 (a Monday) 02:00 UTC = 10:00 in UTC+8
        let (clock, _) = SharedClock::manual(1_792_980_000 * NANOS);
        assert_eq!(clock.local(8 * 3600), (10, 1));
        assert_eq!(clock.local(0), (2, 1));
    }
}
