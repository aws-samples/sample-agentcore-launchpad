import type { FindingsSkin } from "../lib/findings-skin";
import { Btn, Chip, Filters, Notice, Panel, Skeleton } from "./ui";
import { Select } from "./select";

/** V3 kit primitives for the failure-findings panels. */
export const v3FindingsSkin: FindingsSkin = {
  Section: ({ title, children, end }) => <Panel title={title} end={end}>{children}</Panel>,
  Tabs: ({ value, options, onChange }) => <Filters value={value} options={options} onChange={onChange} />,
  Pick: ({ value, options, placeholder, onChange, ariaLabel, testId }) => (
    <Select value={value} onChange={(e) => onChange(e.target.value)} aria-label={ariaLabel} data-testid={testId}>
      <option value="">{placeholder}</option>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </Select>
  ),
  Note: ({ tone, children }) => <Notice s={tone === "error" ? "act" : tone === "warn" ? "wait" : "info"}>{children}</Notice>,
  Busy: () => <Skeleton rows={3} />,
  Button: ({ children, onClick, testId }) => <Btn size="sm" kind="ghost" onClick={onClick} data-testid={testId}>{children}</Btn>,
  Tag: ({ tone, children }) => <Chip s={tone === "red" ? "act" : "off"}>{children}</Chip>,
};
