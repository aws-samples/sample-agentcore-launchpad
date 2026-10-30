//! Action schema + event schema for one workspace's Gateway.

use std::collections::BTreeMap;

use serde_json::Value;

/// The Launchpad template stub: Global's `AgentCore` names (per-tool actions, `Gateway`,
/// `IamEntity`) plus Launchpad's principal types, in the single namespace the MCP schema
/// generator allows.
pub const TEMPLATE: &str = include_str!("../assets/launchpad-template.cedarschema");

/// Global's temporal-session semantics: history pinned to (session id, caller principal).
pub const EVENT_SCHEMA: &str = include_str!("../assets/launchpad-events.dwschema");

#[derive(Debug, thiserror::Error)]
pub enum SchemaError {
    #[error("tools/list manifest: {0}")]
    Manifest(String),
    #[error("tool names {0:?} and {1:?} collide once made identifiers")]
    Collision(String, String),
    #[error("schema generation: {0}")]
    Generate(String),
}

/// The action schema (`.cedarschema` text) for a `tools/list` manifest — one action per
/// tool, typed `context.input` / `context.output`, numbers as `decimal`, every action in
/// `Action::"CallTool"`.
///
/// Gateway tool names are `<target>___<tool>` and targets may contain `-` (the shared
/// `hr-database` target does), which the generator cannot turn into type names. Each name
/// is generated under an identifier-safe alias (`-` → `_`) and the **action ids are
/// restored** to the real tool names afterwards, so policies say
/// `AgentCore::Action::"hr-database___create_payout"` exactly like Global; only the
/// generated record type names keep the alias.
pub fn action_schema(tools_list_json: &str) -> Result<String, SchemaError> {
    let (manifest, aliases) = alias_manifest(tools_list_json)?;
    let mut schema = dogwood_language::mcp_to_cedar_schema_with_template(&manifest, TEMPLATE)
        .map_err(SchemaError::Generate)?;
    let mut targets = std::collections::BTreeSet::new();
    for (alias, original) in &aliases {
        let group = original.split_once(TOOL_SEPARATOR).map(|(t, _)| t.to_string());
        let parents = match &group {
            Some(t) => {
                targets.insert(t.clone());
                format!("in [Action::\"CallTool\", Action::\"{TARGET_GROUP_PREFIX}{t}\"]")
            }
            None => "in [Action::\"CallTool\"]".to_string(),
        };
        schema = schema.replace(
            &format!("action \"{alias}\" in [Action::\"CallTool\"]"),
            &format!("action \"{original}\" {parents}"),
        );
    }
    // one group action per Gateway target: `action in AgentCore::Action::"target:<t>"`
    // keeps the D20 `<target>___*` rules expressible (§13.3)
    let groups: String = targets
        .iter()
        .map(|t| format!("  action \"{TARGET_GROUP_PREFIX}{t}\" in [Action::\"CallTool\"];\n"))
        .collect();
    if let Some(end) = schema.rfind('}') {
        schema.insert_str(end, &groups);
    }
    Ok(schema)
}

/// `<target>___<tool>` — the Gateway's tool-name separator.
pub const TOOL_SEPARATOR: &str = "___";
/// Prefix of the per-target action groups.
pub const TARGET_GROUP_PREFIX: &str = "target:";

/// The Gateway's tool-name grammar (review nit c): `<target>___<tool>` or a bare tool, every
/// `___`-separated segment 1-128 of `[A-Za-z0-9_-]`, never a quote, backslash or other
/// character that could escape the generated schema's string literals. The tool part may
/// itself carry `___`: an MCP-server target prefixes the remote server's own
/// `<server>___<tool>` names with its target name (`paipai___paipai-data___detect_anomaly` on
/// launchpad-gw-example001), so a live manifest has nested separators. The target (group) is
/// always the FIRST segment; the action id is the full name. An empty segment is refused.
pub fn valid_tool_name(name: &str) -> bool {
    let part = |p: &str| (1..=128).contains(&p.len()) && p.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-');
    name.split(TOOL_SEPARATOR).all(part)
}

/// The identifier-safe alias of a tool name.
pub fn alias(tool_name: &str) -> String {
    tool_name.replace('-', "_")
}

fn alias_manifest(tools_list_json: &str) -> Result<(String, BTreeMap<String, String>), SchemaError> {
    let mut doc: Value =
        serde_json::from_str(tools_list_json).map_err(|e| SchemaError::Manifest(e.to_string()))?;
    // accept a bare `tools/list` result, a JSON-RPC response, or a `{tools: [...]}` object
    if doc.get("result").is_none() {
        doc = serde_json::json!({ "result": doc });
    }
    let tools = doc
        .pointer_mut("/result/tools")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| SchemaError::Manifest("no result.tools array".into()))?;
    let mut aliases: BTreeMap<String, String> = BTreeMap::new();
    for tool in tools.iter_mut() {
        let name = tool
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| SchemaError::Manifest("a tool without a name".into()))?
            .to_string();
        if !valid_tool_name(&name) {
            return Err(SchemaError::Manifest(format!(
                "tool name {:?} is outside the Gateway grammar [A-Za-z0-9_-] (segments split by ___, none empty)",
                name.chars().take(80).collect::<String>()
            )));
        }
        let safe = alias(&name);
        if let Some(previous) = aliases.get(&safe) {
            if previous != &name {
                return Err(SchemaError::Collision(previous.clone(), name));
            }
        }
        aliases.insert(safe.clone(), name);
        tool["name"] = Value::String(safe);
    }
    Ok((doc.to_string(), aliases))
}
