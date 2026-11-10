//! A stateless policy subset (Stage A, and the non-recorded part of Stage BT): a Dogwood
//! [`Authorizer`] whose temporal backend is a no-op, so feeding it request events keeps
//! no history and costs no memory.

use dogwood_language::cedar::Schema;
use dogwood_language::{
    Authorizer, Error, Event, EventSignature, LoweredPolicySet, ParsedPolicySet, PolicySchema,
    Response, ServiceSchema, TemporalBindings, TemporalEngine, TemporalField,
};

struct NoHistory;

impl TemporalEngine for NoHistory {
    fn prepare(&mut self, leaves: &[TemporalField], _: &Schema, _: &[EventSignature]) -> Result<(), Error> {
        if leaves.is_empty() {
            Ok(())
        } else {
            Err(Error::Leaf {
                id: leaves[0].id.clone(),
                message: "a stateless set must not contain temporal conditions".into(),
            })
        }
    }
    fn observe(&mut self, _event: &Event) {}
    fn evaluate(&mut self) -> Result<TemporalBindings, String> {
        Ok(TemporalBindings::new())
    }
}

pub struct StatelessSet {
    authorizer: Option<Authorizer>,
}

impl StatelessSet {
    /// Lower `policies` (Dogwood text, no temporal blocks) against the action schema.
    /// An empty text is an empty set: every request is an implicit deny.
    pub fn new(policies: &str, schema: &str, service: &ServiceSchema) -> Result<Self, String> {
        if policies.trim().is_empty() {
            return Ok(StatelessSet { authorizer: None });
        }
        let lowered = lower(policies, schema, service)?;
        let authorizer = Authorizer::builder(lowered)
            .temporal_engine(NoHistory)
            .build()
            .map_err(|e| e.to_string())?;
        Ok(StatelessSet { authorizer: Some(authorizer) })
    }

    pub fn is_empty(&self) -> bool {
        self.authorizer.is_none()
    }

    /// `None` = empty set (implicit deny with no reasons).
    pub fn decide(&mut self, event: &Event) -> Option<Response> {
        self.authorizer.as_mut()?.is_authorized(event)
    }
}

pub fn lower(policies: &str, schema: &str, service: &ServiceSchema) -> Result<LoweredPolicySet, String> {
    let parsed = ParsedPolicySet::parse(policies, service).map_err(|e| e.to_string())?;
    let action_schema = PolicySchema::from_cedarschema_str(schema).map_err(|e| e.to_string())?;
    parsed.lower(&action_schema).map_err(|e| e.to_string())
}
