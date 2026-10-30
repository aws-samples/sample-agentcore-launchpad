//! Which tool actions a statement covers — ONE implementation shared by the compiler
//! (`temporal_referenced_actions`) and the engine (`must_record`), so the two can
//! never disagree (lead review 2026-10-26, defect 2). Coverage follows Cedar's action
//! hierarchy: `CallTool` / `Mcp` contain every tool, `target:<t>` contains the tools of
//! target `<t>`, a tool contains itself.

use std::collections::BTreeSet;

use dogwood_language::{ActionConstraint, ParsedPolicySet, ServiceSchema};

use crate::schema::{TARGET_GROUP_PREFIX, TOOL_SEPARATOR};

/// The tool actions declared in a generated action schema (`action "<tool>" in
/// [Action::"CallTool", …]`), excluding the group actions.
pub fn tool_actions(schema: &str) -> Vec<String> {
    schema
        .lines()
        .filter_map(|line| {
            let rest = line.trim().strip_prefix("action \"")?;
            let (id, tail) = rest.split_once('"')?;
            (tail.contains("Action::\"CallTool\"") && !id.starts_with(TARGET_GROUP_PREFIX))
                .then(|| id.to_string())
        })
        .collect()
}

/// The tools one action entity id stands for in the hierarchy.
pub fn expand(id: &str, all: &[String]) -> Vec<String> {
    if id == "CallTool" || id == "Mcp" {
        all.to_vec()
    } else if let Some(target) = id.strip_prefix(TARGET_GROUP_PREFIX) {
        let prefix = format!("{target}{TOOL_SEPARATOR}");
        all.iter().filter(|a| a.starts_with(&prefix)).cloned().collect()
    } else if all.iter().any(|a| a == id) {
        vec![id.to_string()]
    } else {
        vec![] // an unknown id: no tool
    }
}

/// The tools a statement's action scope covers (`None` = unconstrained: every tool).
pub fn covered_actions(scope: &ActionConstraint, all: &[String]) -> Option<BTreeSet<String>> {
    match scope {
        ActionConstraint::Any => None,
        ActionConstraint::Eq(uid) => Some(expand(uid.id().escaped().as_ref(), all).into_iter().collect()),
        ActionConstraint::In(uids) => {
            Some(uids.iter().flat_map(|u| expand(u.id().escaped().as_ref(), all)).collect())
        }
    }
}

/// Every `AgentCore::Action::"<id>"` literal in a statement's text, expanded through the
/// hierarchy (temporal predicates name the actions whose events they read).
pub fn named_actions(text: &str, all: &[String]) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    let mut rest = text;
    while let Some(pos) = rest.find("AgentCore::Action::\"") {
        let tail = &rest[pos + "AgentCore::Action::\"".len()..];
        let Some(end) = tail.find('"') else { break };
        out.extend(expand(&tail[..end], all));
        rest = &tail[end..];
    }
    out
}

/// The tools whose calls must be recorded because some temporal statement covers them
/// (its action scope) or reads their events (its predicates). Statements that do not parse
/// or are not temporal contribute nothing.
pub fn temporal_referenced<'a>(
    statements: impl IntoIterator<Item = &'a str>,
    all: &[String],
    service: &ServiceSchema,
) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for text in statements {
        let Ok(set) = ParsedPolicySet::parse(text, service) else { continue };
        for q in set.policies() {
            if !q.uses_temporal() {
                continue;
            }
            let source = q.expanded_source();
            out.extend(named_actions(&source, all));
            match covered_actions(&q.scope().action(), all) {
                None => out.extend(all.iter().cloned()),
                Some(cover) => out.extend(cover),
            }
        }
    }
    out
}
