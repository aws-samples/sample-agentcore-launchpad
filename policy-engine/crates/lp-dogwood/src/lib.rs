//! Launchpad's Dogwood core (stream C of plans/DESIGN-POLICY-USER-PRINCIPAL.md §13-§15).
//!
//! * [`schema`]: the per-workspace action schema, generated from the Gateway's `tools/list`
//!   manifest over the Launchpad template (`assets/launchpad-template.cedarschema`), and the
//!   Global-style event schema (`assets/launchpad-events.dwschema`).

pub mod schema;
pub mod events;
pub mod stateless;
pub mod values;
pub mod wire;
pub mod clock;
pub mod workspace;
pub mod engine;

/// Owner shard of a session — identical to `codec.shard_for` on the Python side: the
/// first 8 hex digits of the session key, modulo the shard count.
pub fn shard_for(session: &str, shards: u32) -> u32 {
    let head = session.get(..8).unwrap_or("0");
    (u64::from_str_radix(head, 16).unwrap_or(0) % u64::from(shards.max(1))) as u32
}
pub mod compile;
pub mod simulate;
pub mod bench;
pub mod coverage;
