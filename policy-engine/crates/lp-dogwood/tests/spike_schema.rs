//! The generated demo schema is committed (assets/demo.cedarschema) so non-Rust tests can
//! use it; this golden test keeps it in sync with the generator + template.

#[test]
fn demo_schema_matches_the_committed_golden() {
    let manifest = include_str!("../assets/demo-tools.json");
    let schema = lp_dogwood::schema::action_schema(manifest).expect("schema generation");
    assert!(schema.contains("action \"hr-database___create_payout\" in"), "{schema}");
    assert!(schema.contains("action \"target:hr-approvals\" in"), "{schema}");
    let golden = include_str!("../assets/demo.cedarschema");
    if schema != golden {
        std::fs::write(std::env::temp_dir().join("lp-dogwood-demo.cedarschema"), &schema).unwrap();
        panic!("assets/demo.cedarschema is stale; regenerated copy in $TMPDIR/lp-dogwood-demo.cedarschema");
    }
}
