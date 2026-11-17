import { type AgentForm, type AgentMethod, emptyAgentForm, formFromStoredSpec } from "../../../lib/agent-spec";
import type { AgentTemplateInfo, SharedTemplateInfo, Toolkit } from "../../../lib/api";
import { defaultRuntimeConfig } from "../../../lib/runtime-config";

/**
 * Pure form builders for the two kinds of template (T10 scenario templates, T38 shared
 * templates). Kept out of `TemplateGallery.tsx` so that module exports only components
 * (React fast refresh needs that).
 */

/** The form a template produces, on top of the empty form for its method. */
export function formFromTemplate(template: AgentTemplateInfo): AgentForm {
  const form = emptyAgentForm(template.method);
  return {
    ...form,
    systemPrompt: template.system_prompt,
    toolkits: template.toolkits as Toolkit[],
    longTerm: template.memory_long_term,
    guardrail: template.guardrail ? "anonymize" : "off",
  };
}

/**
 * T38 — the form a *shared* template produces.
 *
 * A shared template's spec is already pruned server-side (no environment ids, nothing
 * secret-shaped), so it is loaded through the same stored-spec reader an edit uses —
 * one mapping, not a second one to drift. Name and display name stay empty on purpose:
 * the consumer is creating their own agent, not a copy that impersonates the source.
 */
export function formFromSharedTemplate(template: SharedTemplateInfo): AgentForm {
  const method = (isAgentMethod(template.method) ? template.method : "harness") as AgentMethod;
  const { form } = formFromStoredSpec(method, "", template.spec);
  return { ...form, name: "", displayName: "", runtimeConfig: form.runtimeConfig ?? defaultRuntimeConfig() };
}

const AGENT_METHODS: AgentMethod[] = ["harness", "zip_runtime", "container", "byoc"];
function isAgentMethod(value: string): value is AgentMethod {
  return (AGENT_METHODS as string[]).includes(value);
}
