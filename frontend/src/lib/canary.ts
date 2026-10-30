// Runtime-canary constants both consoles share (the classic `?view=experiment&mode=canary`
// sub-page and console V2). Lived in `lib/experiments.ts` until the configuration-bundle
// experiment retired (N13-obs2).

// What the online evaluation configs score both arms with when the operator doesn't
// touch the chips — mirrors `app.evaluation.online_evaluators.ONLINE_EVAL_DEFAULT`.
export const ONLINE_EVAL_DEFAULT = ["Builtin.GoalSuccessRate", "Builtin.Helpfulness"];
export const ONLINE_EVAL_MAX = 10; // CreateOnlineEvaluationConfig caps the list at 10
