import { describe, expect, it } from "vitest";

import type { AssistantEvalResource } from "../../../lib/api";
import { evalResourceV2Link } from "./classicUrl";

function res(over: Partial<AssistantEvalResource>): AssistantEvalResource {
  return { kind: "dataset", key: "k", name: "n", status: "ready", ...over } as AssistantEvalResource;
}

describe("evalResourceV2Link", () => {
  it("opens a created dataset on the V2 data center", () => {
    const link = evalResourceV2Link(res({
      kind: "dataset",
      result: { dataset_id: "ds_1", item_count: 3 },
      link: "/evaluation?view=datasets&ds=ds_1",
    }));
    expect(link).toBe("/v2/eval/data?tab=datasets&view=dataset&id=ds_1");
  });

  it("opens created and existing evaluators on the V2 evaluators page", () => {
    expect(evalResourceV2Link(res({ kind: "evaluator", result: { evaluator_id: "acc-ev-1" } })))
      .toBe("/v2/eval/evaluators?view=detail&id=acc-ev-1");
    expect(evalResourceV2Link(res({ kind: "existing", result: { evaluator_id: "Builtin.Helpfulness" } })))
      .toBe("/v2/eval/evaluators?view=detail&id=Builtin.Helpfulness");
  });

  it("has no link before the resource has an id, or for other kinds", () => {
    expect(evalResourceV2Link(res({ kind: "dataset", result: null }))).toBeNull();
    expect(evalResourceV2Link(res({ kind: "evaluator", result: {} }))).toBeNull();
    expect(evalResourceV2Link(res({ kind: "lambda_function", result: { function_arn: "arn:x" } }))).toBeNull();
  });
});
