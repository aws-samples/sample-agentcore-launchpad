# Octank China — Travel and Expense Policy (2026)

Synthetic test fixture for the Launchpad-CN end-to-end scenario suite; it describes no real
company.

## Filing deadline and approval

- An expense claim must be filed in the expense portal within **45 calendar days** of the last
  day of travel. Claims filed later are rejected unless the CFO grants an exception.
- Every claim is approved by the employee's **line manager**. Claims above ¥10,000 need a second
  approval from the department head.
- Approved claims are reimbursed with the next payroll run, at the latest within **10 business
  days** of approval.

## Receipts

- Receipts (fapiao) are required for every item above **¥50**.
- Lost receipts must be declared with a signed statement; at most two such items per claim.

## Daily caps

| Category | Cap |
|---|---|
| Meals | ¥150 per day |
| Domestic lodging | ¥600 per night (tier-1 cities ¥800) |
| Local transport | ¥120 per day, taxis only with a receipt |

## Report format

An expense report lists the employee id, the travel period and one line per item with
category, amount, currency and whether a receipt is attached, followed by the total.
Reports are drafted with the `expense-report-writer` skill where available.
