//! JSON (as the interceptor sends it) → Dogwood [`Value`], typed by the generated schema.
//!
//! JSON has one number type; Cedar has `Long` and `decimal`. The action schema decides:
//! a field typed `Long` becomes [`Value::Int`] (a fractional number there is a type error
//! and is dropped, which fails a policy that reads it closed), anything else numeric
//! becomes a [`Value::Decimal`] rendered with 1-4 fractional digits (Cedar's decimal
//! literal range).

use std::collections::BTreeMap;

use dogwood_language::Value;
use serde_json::Value as Json;

/// Field path (`["input", "amount"]`) → the Cedar type text the generator declared.
pub type FieldTypes = BTreeMap<Vec<String>, String>;

pub fn to_value(json: &Json, path: &mut Vec<String>, types: &FieldTypes) -> Option<Value> {
    Some(match json {
        Json::Null => return None,
        Json::Bool(b) => Value::Bool(*b),
        Json::String(s) => Value::String(s.clone()),
        Json::Number(n) => {
            let declared = types.get(path.as_slice()).map(String::as_str).unwrap_or("");
            if declared.starts_with("Long") {
                Value::Int(n.as_i64()?)
            } else if let Some(i) = n.as_i64().filter(|_| declared.is_empty() && n.is_i64()) {
                // an undeclared integer (e.g. an entity attribute): keep it a Long
                Value::Int(i)
            } else {
                Value::Decimal(decimal_text(n.as_f64()?)?)
            }
        }
        Json::Array(items) => Value::Array(
            items.iter().filter_map(|item| to_value(item, path, types)).collect(),
        ),
        Json::Object(map) => {
            let mut out = BTreeMap::new();
            for (key, item) in map {
                path.push(key.clone());
                if let Some(v) = to_value(item, path, types) {
                    out.insert(key.clone(), v);
                }
                path.pop();
            }
            Value::Object(out)
        }
    })
}

/// A finite float as a Cedar decimal literal (`420` → `"420.0"`, at most 4 fraction
/// digits, range ±922337203685477.5807). `None` when it cannot be represented.
pub fn decimal_text(x: f64) -> Option<String> {
    if !x.is_finite() || x.abs() > 922_337_203_685_477.0 {
        return None;
    }
    let mut text = format!("{x:.4}");
    while text.ends_with('0') && !text.ends_with(".0") {
        text.pop();
    }
    Some(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_follow_the_declared_type() {
        let mut types = FieldTypes::new();
        types.insert(vec!["input".into(), "cents".into()], "Long".into());
        types.insert(vec!["input".into(), "amount".into()], "decimal".into());
        let json = serde_json::json!({"cents": 1234, "amount": 420, "name": "x", "skip": null});
        let mut path = vec!["input".to_string()];
        let Some(Value::Object(map)) = to_value(&json, &mut path, &types) else { panic!() };
        assert_eq!(map["cents"], Value::Int(1234));
        assert_eq!(map["amount"], Value::Decimal("420.0".into()));
        assert_eq!(map["name"], Value::String("x".into()));
        assert!(!map.contains_key("skip"));
    }

    #[test]
    fn decimal_text_is_a_cedar_literal() {
        assert_eq!(decimal_text(420.5).as_deref(), Some("420.5"));
        assert_eq!(decimal_text(0.12346).as_deref(), Some("0.1235"));
        assert_eq!(decimal_text(f64::NAN), None);
    }
}
