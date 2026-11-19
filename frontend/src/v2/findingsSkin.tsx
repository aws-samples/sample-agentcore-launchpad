import type { FindingsSkin } from "../lib/findings-skin";
import { Alert, Button, Card, Segmented, Select, Spin, Tag } from "./ui";

/** V2 kit primitives for the failure-findings panels. */
export const v2FindingsSkin: FindingsSkin = {
  Section: ({ title, children, testId, end }) => (
    <Card title={title} end={end} testId={testId}>{children}</Card>
  ),
  Tabs: ({ value, options, onChange, ariaLabel }) => (
    <Segmented value={value} options={options} onChange={onChange} ariaLabel={ariaLabel} />
  ),
  Pick: ({ value, options, placeholder, onChange, ariaLabel, testId }) => (
    <Select value={value} options={options} placeholder={placeholder} onChange={onChange} ariaLabel={ariaLabel} testId={testId} />
  ),
  Note: ({ tone, children, action }) => <Alert tone={tone} action={action}>{children}</Alert>,
  Busy: () => <Spin />,
  Button: ({ children, onClick, testId }) => <Button size="sm" onClick={onClick} testId={testId}>{children}</Button>,
  Tag: ({ tone, children }) => <Tag tone={tone}>{children}</Tag>,
};
