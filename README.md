# BlackRoad Payroll System

[![License: Proprietary](https://img.shields.io/badge/License-Proprietary-red.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/Tests-15%20passing-brightgreen.svg)](tests/)
[![BlackRoad OS](https://img.shields.io/badge/BlackRoad-OS-black.svg)](https://github.com/BlackRoad-OS)

> Full-cycle payroll processing: net pay calculation, paystub generation, FICA withholding, W-2 year-end summaries, bulk runs, and Stripe-powered disbursement — all in a single Python library.

Part of the [BlackRoad OS](https://github.com/BlackRoad-OS) platform.

---

## Table of Contents

1. [Overview](#overview)
2. [Features](#features)
3. [Installation](#installation)
4. [Quick Start](#quick-start)
5. [CLI Reference](#cli-reference)
6. [Python API Reference](#python-api-reference)
   - [PayrollService](#payrollservice)
   - [PayrollDB](#payrolldb)
   - [Data Models](#data-models)
   - [Enumerations](#enumerations)
7. [Stripe Integration](#stripe-integration)
8. [npm / JavaScript](#npm--javascript)
9. [Testing](#testing)
10. [Architecture](#architecture)
11. [Production Deployment](#production-deployment)
12. [License](#license)

---

## Overview

BlackRoad Payroll System is a production-grade payroll engine built for reliability, accuracy, and extensibility. It handles the full payroll lifecycle — from onboarding an employee through generating paystubs, withholding taxes, and producing year-end W-2 summaries — with optional Stripe-powered fund disbursement.

Key design principles:

- **Decimal-exact arithmetic** — every dollar amount uses Python `Decimal` to eliminate floating-point rounding errors.
- **Immutable audit trail** — every paystub is persisted to SQLite; nothing is deleted.
- **Composable service layer** — `PayrollService` sits on top of `PayrollDB`, making it easy to swap storage backends or wrap with an HTTP API.
- **2024-compliant tax tables** — SS wage base ($168,600), 2024 federal brackets (single & MFJ), standard deductions, and additional Medicare surtax.

---

## Features

| Category | Detail |
|---|---|
| **Net pay calculation** | Gross → pre-tax deductions → FICA → FIT → state → net |
| **Paystubs** | Check numbers, YTD tracking, itemized line items, persistent storage |
| **Federal taxes** | 2024 brackets (single & MFJ), W-4 allowances, standard deduction |
| **FICA** | Social Security (6.2%, wage base cap) + Medicare (1.45% + 0.9% surtax) |
| **State tax** | Configurable flat rate per state (extensible to full state tables) |
| **Deduction types** | Pre-tax: 401k, HSA, FSA, health; Post-tax: Roth, garnishment, other |
| **Pay frequencies** | Weekly (52), biweekly (26), semi-monthly (24), monthly (12) |
| **Bulk processing** | Single-call payroll run for all active employees |
| **Year-end W-2** | Box 1 (FIT wages), Box 3 (SS wages), Box 4, Box 5, Box 6 |
| **CSV export** | Full payroll data export for any calendar year |
| **Stripe disbursement** | Net pay funded via Stripe Payouts or Treasury (see [Stripe Integration](#stripe-integration)) |
| **CLI** | `payroll` command — add employees, run payroll, export reports |

---

## Installation

### Python (pip)

```bash
pip install blackroad-payroll
```

Or from source:

```bash
git clone https://github.com/BlackRoad-OS/blackroad-payroll-system.git
cd blackroad-payroll-system
pip install -e .
```

**Requirements:** Python 3.9 or later. No external runtime dependencies beyond the standard library.

### Node.js / npm

A thin JavaScript wrapper is available for teams running Node-based back-ends:

```bash
npm install @blackroad/payroll
```

The npm package shells out to the Python engine and exposes an async Promise-based API. See [npm / JavaScript](#npm--javascript) for details.

---

## Quick Start

### Python

```python
from decimal import Decimal
from payroll_system import PayrollDB, PayrollService, PayFrequency, PayPeriod

# Initialise (SQLite auto-created at ~/.blackroad/payroll.db)
svc = PayrollService()

# Add employee
emp = svc.add_employee(
    name="Alice Smith",
    salary=Decimal("80000"),
    pay_frequency=PayFrequency.BIWEEKLY,
    title="Engineer",
    state="CA",
)
print(emp.id)          # EMP-XXXXXXXX
print(emp.period_gross)  # 3076.92

# Generate paystub
period = PayPeriod("2024-01-01", "2024-01-14", "2024-01-19")
stub = svc.generate_paystub(emp, period)
print(f"Net pay: ${stub.net_pay:,.2f}")

# Bulk payroll — all active employees
stubs = svc.bulk_process(svc.db.list_employees(), period)

# Year-end W-2
summary = svc.year_end_summary(emp.id, 2024)
print(f"W-2 Box 1: ${summary.w2_box1:,.2f}")
```

### CLI

```bash
# Add employee
payroll add-employee "Alice Smith" 80000 --freq biweekly --title Engineer --state CA

# Run individual payroll
payroll run EMP-XXXXXXXX --start 2024-01-01 --end 2024-01-14 --paydate 2024-01-19

# Bulk payroll for all active employees
payroll bulk --start 2024-01-01 --end 2024-01-14 --paydate 2024-01-19

# Year-end W-2
payroll w2 EMP-XXXXXXXX --year 2024

# List all employees
payroll list

# Export payroll CSV
payroll export --year 2024

# Add 401(k) deduction (3% of gross)
payroll deduction EMP-XXXXXXXX pre_tax_401k 3 --pct --desc "401(k) Contribution"
```

---

## CLI Reference

| Command | Arguments | Description |
|---|---|---|
| `add-employee` | `name salary` | Add a new salaried employee |
| `run` | `employee_id --start --end --paydate` | Generate paystub for one employee |
| `bulk` | `--start --end --paydate` | Run payroll for all active employees |
| `w2` | `employee_id --year` | Print year-end W-2 summary |
| `list` | `[--status active\|terminated\|on_leave]` | List employees |
| `export` | `[--year YYYY]` | Export payroll CSV to stdout |
| `deduction` | `employee_id type amount` | Add a recurring deduction |

**Global flags:**

| Flag | Default | Description |
|---|---|---|
| `--db PATH` | `~/.blackroad/payroll.db` | Path to SQLite database file |

**Deduction types:** `pre_tax_401k`, `pre_tax_hsa`, `pre_tax_fsa`, `pre_tax_health`, `post_tax_roth`, `post_tax_garnishment`, `post_tax_other`

---

## Python API Reference

### PayrollService

The primary service class. Instantiate with an optional `PayrollDB`.

```python
svc = PayrollService()                       # default DB path
svc = PayrollService(PayrollDB("/data/payroll.db"))
```

#### `add_employee`

```python
svc.add_employee(
    name: str,
    salary: Decimal,
    pay_frequency: PayFrequency = PayFrequency.BIWEEKLY,
    filing_status: str = "single",           # "single" | "married" | "head_of_household" (IRS W-4 filing status)
    w4_allowances: int = 1,
    state: str = "CA",
    department: str = "",
    title: str = "",
    email: str = "",
    hourly_rate: Optional[Decimal] = None,   # set for hourly employees
) -> Employee
```

#### `calculate_net_pay`

```python
svc.calculate_net_pay(
    employee: Employee,
    hours: Optional[Decimal] = None,         # required for hourly employees
    overtime_hours: Optional[Decimal] = None,
) -> dict
# Returns: gross, taxable_gross, pre_tax_deductions, federal_tax, ss_tax,
#          medicare_tax, state_tax, post_tax_deductions, net
```

#### `generate_paystub`

```python
svc.generate_paystub(
    employee: Employee,
    period: PayPeriod,
    hours: Optional[Decimal] = None,
    overtime_hours: Optional[Decimal] = None,
) -> Paystub
```

Generates, persists, and returns a `Paystub`. Updates YTD accumulators.

#### `withhold_taxes`

```python
svc.withhold_taxes(
    gross: Decimal,
    annual_gross: Decimal,
    filing_status: str = "single",
    w4_allowances: int = 1,
    ytd_ss: Decimal = Decimal("0"),
) -> Tuple[Decimal, Decimal, Decimal]
# Returns: (federal_income_tax, ss_tax, medicare_tax)
```

#### `bulk_process`

```python
svc.bulk_process(employees: List[Employee], period: PayPeriod) -> List[Paystub]
```

Processes payroll for a list of employees. Skips and logs failures without aborting the run.

#### `year_end_summary`

```python
svc.year_end_summary(employee_id: str, year: int) -> YearEndSummary
```

Aggregates all paystubs for the given year and computes W-2 boxes 1, 3, 4, 5, and 6.

#### `export_payroll_csv`

```python
svc.export_payroll_csv(year: int) -> str  # CSV string
```

---

### PayrollDB

Low-level SQLite persistence layer. All methods are transactional.

| Method | Signature | Description |
|---|---|---|
| `save_employee` | `(Employee) -> Employee` | Upsert employee record |
| `get_employee` | `(emp_id: str) -> Optional[Employee]` | Fetch by ID |
| `list_employees` | `(status=None) -> List[Employee]` | List all (or by status) |
| `update_ytd` | `(emp_id, gross, federal, ss, medicare, deductions)` | Increment YTD totals |
| `save_paystub` | `(Paystub)` | Persist a paystub |
| `get_paystubs` | `(emp_id, year=None) -> List[Paystub]` | Fetch paystubs |
| `save_deduction` | `(Deduction)` | Upsert a deduction |
| `get_deductions` | `(emp_id) -> List[Deduction]` | Active deductions for employee |

**SQLite indexes** (auto-created on first run):

```sql
idx_paystubs_employee  ON paystubs(employee_id, pay_date)
idx_deductions_employee ON deductions(employee_id, active)
```

---

### Data Models

#### `Employee`

| Field | Type | Description |
|---|---|---|
| `id` | `str` | `EMP-XXXXXXXX` UUID prefix |
| `name` | `str` | Full legal name |
| `salary` | `Decimal` | Annual salary |
| `hourly_rate` | `Optional[Decimal]` | Hourly rate (if hourly) |
| `pay_frequency` | `PayFrequency` | Pay schedule |
| `filing_status` | `str` | W-4 filing status |
| `w4_allowances` | `int` | W-4 allowance count |
| `state` | `str` | Two-letter state code |
| `status` | `EmployeeStatus` | `active`, `terminated`, `on_leave` |
| `hire_date` | `date` | Date of hire |
| `department` | `str` | Department name |
| `title` | `str` | Job title |
| `email` | `str` | Work email |
| `ytd_gross` | `Decimal` | Year-to-date gross pay |
| `ytd_federal_tax` | `Decimal` | Year-to-date federal withholding |
| `ytd_ss_tax` | `Decimal` | Year-to-date Social Security |
| `ytd_medicare_tax` | `Decimal` | Year-to-date Medicare |
| `ytd_deductions` | `Decimal` | Year-to-date total deductions |

#### `Paystub`

| Field | Type | Description |
|---|---|---|
| `id` | `str` | UUID |
| `check_number` | `str` | `CHK-XXXXXX` |
| `pay_period` | `PayPeriod` | `start_date`, `end_date`, `pay_date` |
| `gross_pay` | `Decimal` | Period gross |
| `federal_income_tax` | `Decimal` | FIT withheld |
| `state_income_tax` | `Decimal` | State withheld |
| `ss_tax` | `Decimal` | FICA Social Security |
| `medicare_tax` | `Decimal` | FICA Medicare |
| `pre_tax_deductions` | `Decimal` | 401k/HSA/FSA/health |
| `post_tax_deductions` | `Decimal` | Roth/garnishment |
| `net_pay` | `Decimal` | Take-home amount |
| `ytd_gross` | `Decimal` | Running YTD gross |
| `lines` | `List[PaystubLine]` | Itemized line items |

#### `YearEndSummary`

| Field | W-2 Box | Description |
|---|---|---|
| `w2_box1` | Box 1 | Wages subject to FIT (gross − pre-tax deductions) |
| `w2_box3` | Box 3 | Social Security wages (capped at $168,600) |
| `w2_box5` | Box 5 | Medicare wages and tips |
| `ytd_federal_tax` | Box 2 | Federal income tax withheld |
| `ytd_ss_tax` | Box 4 | Social Security tax withheld |
| `ytd_medicare_tax` | Box 6 | Medicare tax withheld |

#### `Deduction`

| Field | Type | Description |
|---|---|---|
| `id` | `str` | UUID |
| `employee_id` | `str` | Foreign key |
| `deduction_type` | `DeductionType` | See enumerations below |
| `amount` | `Decimal` | Per-period dollar amount (or % of gross) |
| `is_percentage` | `bool` | If `True`, `amount` is a percentage |
| `active` | `bool` | Soft-delete flag |

---

### Enumerations

#### `PayFrequency`

| Value | `periods_per_year` |
|---|---|
| `weekly` | 52 |
| `biweekly` | 26 |
| `semi_monthly` | 24 |
| `monthly` | 12 |

#### `EmployeeStatus`

`active` · `terminated` · `on_leave`

#### `DeductionType`

| Value | Category |
|---|---|
| `pre_tax_401k` | Pre-tax |
| `pre_tax_hsa` | Pre-tax |
| `pre_tax_fsa` | Pre-tax |
| `pre_tax_health` | Pre-tax |
| `post_tax_roth` | Post-tax |
| `post_tax_garnishment` | Post-tax |
| `post_tax_other` | Post-tax |

---

## Stripe Integration

BlackRoad Payroll System calculates exact net pay amounts; Stripe handles the money movement. The recommended integration pattern is:

### 1. Install the Stripe library

```bash
pip install stripe
```

### 2. Disburse net pay via Stripe Payouts

After generating paystubs, iterate and create a Stripe Transfer or Payout for each employee's net pay:

```python
import stripe
from decimal import Decimal
from payroll_system import PayrollService, PayPeriod

stripe.api_key = "sk_live_..."   # Use environment variable in production

svc = PayrollService()
period = PayPeriod("2024-01-01", "2024-01-14", "2024-01-19")
employees = svc.db.list_employees()
stubs = svc.bulk_process(employees, period)

for stub in stubs:
    # Convert to cents — Stripe amounts are integers
    amount_cents = int((stub.net_pay * 100).to_integral_value())

    stripe.Transfer.create(
        amount=amount_cents,
        currency="usd",
        destination=employee_stripe_account_id,  # Stripe connected account ID — store separately; NOT the internal EMP-* id
        description=f"Payroll {stub.pay_period.pay_date} — {stub.check_number}",
        metadata={
            "employee_id": stub.employee_id,
            "check_number": stub.check_number,
            "pay_date": str(stub.pay_period.pay_date),
        },
    )
```

### 3. Stripe Connect — contractor payments

For 1099 contractors, use Stripe Connect with `instant_payouts` enabled:

```python
stripe.Payout.create(
    amount=amount_cents,
    currency="usd",
    method="instant",        # optional — requires instant payout eligibility
    description=stub.check_number,
    stripe_account=contractor_stripe_account_id,
)
```

### 4. Webhook reconciliation

Listen to `payout.paid`, `payout.failed`, and `transfer.reversed` events to keep your payroll records in sync:

```python
@app.post("/stripe/webhook")
def stripe_webhook(request):
    event = stripe.Webhook.construct_event(
        request.body, request.headers["Stripe-Signature"], endpoint_secret
    )
    if event["type"] == "payout.paid":
        # Mark stub disbursed in your DB
        pass
    elif event["type"] == "payout.failed":
        # Alert payroll admin
        pass
```

### Environment variables

| Variable | Description |
|---|---|
| `STRIPE_SECRET_KEY` | Stripe secret API key (`sk_live_...` / `sk_test_...`) |
| `STRIPE_WEBHOOK_SECRET` | Webhook endpoint signing secret (`whsec_...`) |

> **Security:** Never hard-code API keys. Use a secrets manager (AWS Secrets Manager, HashiCorp Vault, or environment variables injected at runtime).

---

## npm / JavaScript

An npm package for Node.js and browser-compatible environments is available:

```bash
npm install @blackroad/payroll
```

It provides a Promise-based API that delegates to the Python engine via a local subprocess or a deployed REST API:

```js
import { PayrollClient } from "@blackroad/payroll";

const client = new PayrollClient({ baseUrl: "https://payroll.yourcompany.com" });

// Add employee
const employee = await client.addEmployee({
  name: "Alice Smith",
  salary: 80000,
  payFrequency: "biweekly",
  state: "CA",
});

// Generate paystub
const stub = await client.generatePaystub(employee.id, {
  startDate: "2024-01-01",
  endDate:   "2024-01-14",
  payDate:   "2024-01-19",
});

console.log(`Net pay: $${stub.netPay}`);

// Bulk payroll
const stubs = await client.bulkProcess({
  startDate: "2024-01-01",
  endDate:   "2024-01-14",
  payDate:   "2024-01-19",
});
```

### TypeScript types

Full TypeScript definitions are included in the package. Import from `@blackroad/payroll/types`.

---

## Testing

### Run the full test suite

```bash
pip install pytest
pytest tests/ -v
```

### Expected output

```
tests/test_payroll_system.py::test_add_employee                  PASSED
tests/test_payroll_system.py::test_employee_period_gross_biweekly PASSED
tests/test_payroll_system.py::test_employee_period_gross_monthly  PASSED
tests/test_payroll_system.py::test_generate_paystub_salaried     PASSED
tests/test_payroll_system.py::test_generate_paystub_check_number PASSED
tests/test_payroll_system.py::test_generate_paystub_updates_ytd  PASSED
tests/test_payroll_system.py::test_calculate_net_pay_deductions  PASSED
tests/test_payroll_system.py::test_withhold_taxes_fica_ss_cap    PASSED
tests/test_payroll_system.py::test_withhold_taxes_zero_income    PASSED
tests/test_payroll_system.py::test_inactive_employee_paystub_raises PASSED
tests/test_payroll_system.py::test_bulk_process                  PASSED
tests/test_payroll_system.py::test_year_end_summary              PASSED
tests/test_payroll_system.py::test_export_payroll_csv            PASSED
tests/test_payroll_system.py::test_net_pay_is_positive           PASSED
tests/test_payroll_system.py::test_list_employees                PASSED

15 passed in 0.25s
```

### Test coverage

| Area | Tests |
|---|---|
| Employee CRUD | `test_add_employee`, `test_list_employees` |
| Period gross | `test_employee_period_gross_biweekly`, `test_employee_period_gross_monthly` |
| Paystub generation | `test_generate_paystub_salaried`, `test_generate_paystub_check_number`, `test_generate_paystub_updates_ytd` |
| Net pay & deductions | `test_calculate_net_pay_deductions`, `test_net_pay_is_positive` |
| FICA withholding | `test_withhold_taxes_fica_ss_cap`, `test_withhold_taxes_zero_income` |
| Validation | `test_inactive_employee_paystub_raises` |
| Bulk processing | `test_bulk_process` |
| Year-end W-2 | `test_year_end_summary` |
| CSV export | `test_export_payroll_csv` |

### End-to-end verification

The following e2e flow exercises the entire stack — add employee → add deductions → run bulk payroll → generate W-2 → export CSV:

```bash
# Start fresh
export PAYROLL_DB=/tmp/e2e_payroll.db

payroll --db $PAYROLL_DB add-employee "Test Employee" 100000 --freq biweekly --title "QA Engineer" --state NY
# Copy EMP-XXXXXXXX from output

payroll --db $PAYROLL_DB deduction EMP-XXXXXXXX pre_tax_401k 5 --pct --desc "401(k) 5%"
payroll --db $PAYROLL_DB run EMP-XXXXXXXX --start 2024-01-01 --end 2024-01-14 --paydate 2024-01-19
payroll --db $PAYROLL_DB w2 EMP-XXXXXXXX --year 2024
payroll --db $PAYROLL_DB export --year 2024
```

---

## Architecture

```
blackroad-payroll-system/
├── src/
│   └── payroll_system.py     # 920+ lines — entire engine
│       ├── Constants          FICA rates, 2024 federal brackets, standard deductions
│       ├── Enumerations       PayFrequency, EmployeeStatus, DeductionType
│       ├── Data Models        Employee, PayPeriod, Paystub, YearEndSummary, Deduction
│       ├── PayrollDB          SQLite persistence, WAL mode, FK enforcement
│       ├── PayrollService     Business logic — net pay, tax withholding, W-2
│       └── CLI (argparse)     payroll add-employee | run | bulk | w2 | list | export | deduction
└── tests/
    └── test_payroll_system.py  15 pytest tests
```

### Database schema

```sql
employees   (id PK, name, salary, pay_frequency, filing_status, state, status, ytd_*)
paystubs    (id PK, employee_id FK, period_start, period_end, gross_pay, net_pay, ...)
deductions  (id PK, employee_id FK, deduction_type, amount, is_percentage, active)

-- Indexes
idx_paystubs_employee   ON paystubs(employee_id, pay_date)
idx_deductions_employee ON deductions(employee_id, active)
```

### Tax calculation flow

```
gross_pay
  └─ − pre_tax_deductions (401k, HSA, FSA, health)
       = taxable_gross
         ├─ federal_income_tax  (2024 brackets, W-4 allowances, annualized method)
         ├─ ss_tax              (6.2%, SS wage base $168,600 cap)
         ├─ medicare_tax        (1.45% + 0.9% surtax above $200k)
         └─ state_income_tax    (configurable flat rate)
  └─ − post_tax_deductions (Roth, garnishment)
     = net_pay
```

---

## Production Deployment

### Recommended stack

| Component | Recommendation |
|---|---|
| Database | PostgreSQL (replace `PayrollDB` SQLite backend for multi-tenant) |
| API layer | FastAPI or Django REST Framework wrapping `PayrollService` |
| Auth | JWT + RBAC (payroll admin, manager, read-only roles) |
| Secrets | AWS Secrets Manager / HashiCorp Vault for Stripe keys |
| Logging | Structured JSON logs (configure `logging` handler) |
| CI/CD | GitHub Actions → run `pytest tests/` on every push |

### Security checklist

- [ ] Rotate Stripe API keys (`STRIPE_SECRET_KEY`) via secrets manager — never commit to source.
- [ ] Restrict database access to the payroll service user only.
- [ ] Enable SQLite WAL mode (already enabled) or migrate to PostgreSQL for concurrent writes.
- [ ] Audit log every paystub generation (`logger.info` calls are in place).
- [ ] Enforce HTTPS on any API endpoint exposing payroll data.
- [ ] Validate employee IDs before Stripe transfers to prevent misdirected payments.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `PAYROLL_DB` | No | Override default `~/.blackroad/payroll.db` path |
| `STRIPE_SECRET_KEY` | For disbursement | Stripe secret key |
| `STRIPE_WEBHOOK_SECRET` | For reconciliation | Webhook signing secret |

---

## License

Proprietary — © BlackRoad OS, Inc. All rights reserved.

For licensing inquiries contact [BlackRoad OS](https://github.com/BlackRoad-OS).
