# BlackRoad Payroll System

> Full-cycle payroll: net pay calculation, paystub generation, FICA withholding, W2 year-end summaries, and bulk processing.

Part of the [BlackRoad OS](https://github.com/BlackRoad-OS) platform.

## Features

- **Net pay**: `calculate_net_pay()` — gross → pre-tax deductions → FICA → FIT → state → net
- **Paystubs**: `generate_paystub()` with check numbers, YTD tracking, itemized lines
- **Tax withholding**: Federal (2024 brackets), Social Security (SS wage base cap), Medicare (additional 0.9%)
- **Deduction types**: Pre-tax 401k/HSA/FSA/health, post-tax Roth/garnishment
- **Bulk processing**: `bulk_process()` for entire payroll runs
- **Year-end W2**: Box 1, 3, 4, 5, 6 calculations
- **Pay frequencies**: Weekly, biweekly, semi-monthly, monthly

## Usage

```bash
# Add employee
python src/payroll_system.py add-employee "Alice Smith" 80000 --freq biweekly --title Engineer

# Run individual payroll
python src/payroll_system.py run EMP-XXXXXXXX --start 2024-01-01 --end 2024-01-14 --paydate 2024-01-19

# Bulk payroll
python src/payroll_system.py bulk --start 2024-01-01 --end 2024-01-14 --paydate 2024-01-19

# Year-end W2
python src/payroll_system.py w2 EMP-XXXXXXXX --year 2024

# Add 401k deduction (3% of gross)
python src/payroll_system.py deduction EMP-XXXXXXXX pre_tax_401k 3 --pct --desc "401(k)"
```

## Architecture

- `src/payroll_system.py` — 927+ lines: `Employee`, `PayPeriod`, `Paystub`, `PayrollDB`, `PayrollService`
- `tests/` — 16 test functions covering FICA, deductions, W2, bulk processing
- SQLite: `employees` + `paystubs` + `deductions` tables

## License

Proprietary — © BlackRoad OS, Inc. All rights reserved.
