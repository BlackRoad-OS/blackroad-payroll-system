"""
BlackRoad Payroll System
=========================
Production-quality payroll processing with net pay calculation,
paystub generation, tax withholding, year-end W2 summaries,
deductions management, and bulk processing. SQLite persistence.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import Dict, Generator, List, Optional, Tuple

DB_PATH = Path.home() / ".blackroad" / "payroll.db"
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("payroll")

PRECISION = Decimal("0.01")

# ─── FICA / Federal Constants (2024) ─────────────────────────────────────────
SS_RATE = Decimal("0.062")
SS_WAGE_BASE = Decimal("168600")
MEDICARE_RATE = Decimal("0.0145")
ADDITIONAL_MEDICARE_RATE = Decimal("0.009")
ADDITIONAL_MEDICARE_THRESHOLD = Decimal("200000")

# Standard annual allowance per W4 allowance claimed
W4_ALLOWANCE_2024 = Decimal("4300")

# 2024 federal tax brackets (single) for withholding tables
FEDERAL_BRACKETS_SINGLE_2024 = [
    (Decimal("0"), Decimal("11600"), Decimal("0.10")),
    (Decimal("11600"), Decimal("47150"), Decimal("0.12")),
    (Decimal("47150"), Decimal("100525"), Decimal("0.22")),
    (Decimal("100525"), Decimal("191950"), Decimal("0.24")),
    (Decimal("191950"), Decimal("243725"), Decimal("0.32")),
    (Decimal("243725"), Decimal("609350"), Decimal("0.35")),
    (Decimal("609350"), None, Decimal("0.37")),
]

FEDERAL_BRACKETS_MFJ_2024 = [
    (Decimal("0"), Decimal("23200"), Decimal("0.10")),
    (Decimal("23200"), Decimal("94300"), Decimal("0.12")),
    (Decimal("94300"), Decimal("201050"), Decimal("0.22")),
    (Decimal("201050"), Decimal("383900"), Decimal("0.24")),
    (Decimal("383900"), Decimal("487450"), Decimal("0.32")),
    (Decimal("487450"), Decimal("731200"), Decimal("0.35")),
    (Decimal("731200"), None, Decimal("0.37")),
]

STANDARD_DEDUCTIONS = {
    "single": Decimal("14600"),
    "married": Decimal("29200"),
    "head_of_household": Decimal("21900"),
}


# ─── Enumerations ─────────────────────────────────────────────────────────────
class PayFrequency(str, Enum):
    WEEKLY = "weekly"          # 52 per year
    BIWEEKLY = "biweekly"      # 26 per year
    SEMI_MONTHLY = "semi_monthly"  # 24 per year
    MONTHLY = "monthly"        # 12 per year

    @property
    def periods_per_year(self) -> int:
        return {"weekly": 52, "biweekly": 26, "semi_monthly": 24, "monthly": 12}[self.value]


class EmployeeStatus(str, Enum):
    ACTIVE = "active"
    TERMINATED = "terminated"
    ON_LEAVE = "on_leave"


class DeductionType(str, Enum):
    PRE_TAX_401K = "pre_tax_401k"
    PRE_TAX_HSA = "pre_tax_hsa"
    PRE_TAX_FSA = "pre_tax_fsa"
    PRE_TAX_HEALTH = "pre_tax_health"
    POST_TAX_ROTH = "post_tax_roth"
    POST_TAX_GARNISHMENT = "post_tax_garnishment"
    POST_TAX_OTHER = "post_tax_other"


# ─── Data Classes ─────────────────────────────────────────────────────────────
@dataclass
class Deduction:
    id: str
    employee_id: str
    deduction_type: DeductionType
    amount: Decimal          # Per-period amount
    description: str
    is_percentage: bool = False  # If True, amount is % of gross
    active: bool = True

    def __post_init__(self):
        if isinstance(self.amount, (int, float, str)):
            self.amount = Decimal(str(self.amount))
        if isinstance(self.deduction_type, str):
            self.deduction_type = DeductionType(self.deduction_type)


@dataclass
class Employee:
    id: str
    name: str
    salary: Decimal           # Annual salary
    hourly_rate: Optional[Decimal]  # If hourly employee
    pay_frequency: PayFrequency
    filing_status: str        # single, married, head_of_household
    w4_allowances: int
    state: str
    status: EmployeeStatus
    hire_date: date
    department: str = ""
    title: str = ""
    email: str = ""
    ytd_gross: Decimal = Decimal("0")
    ytd_federal_tax: Decimal = Decimal("0")
    ytd_ss_tax: Decimal = Decimal("0")
    ytd_medicare_tax: Decimal = Decimal("0")
    ytd_deductions: Decimal = Decimal("0")

    def __post_init__(self):
        for attr in ("salary", "ytd_gross", "ytd_federal_tax", "ytd_ss_tax",
                     "ytd_medicare_tax", "ytd_deductions"):
            val = getattr(self, attr)
            if isinstance(val, (int, float, str)):
                setattr(self, attr, Decimal(str(val)))
        if self.hourly_rate is not None and isinstance(self.hourly_rate, (int, float, str)):
            self.hourly_rate = Decimal(str(self.hourly_rate))
        if isinstance(self.pay_frequency, str):
            self.pay_frequency = PayFrequency(self.pay_frequency)
        if isinstance(self.status, str):
            self.status = EmployeeStatus(self.status)
        if isinstance(self.hire_date, str):
            self.hire_date = date.fromisoformat(self.hire_date)

    @property
    def is_hourly(self) -> bool:
        return self.hourly_rate is not None

    @property
    def period_gross(self) -> Decimal:
        return (self.salary / self.pay_frequency.periods_per_year).quantize(PRECISION)


@dataclass
class PayPeriod:
    start_date: date
    end_date: date
    pay_date: date

    def __post_init__(self):
        for attr in ("start_date", "end_date", "pay_date"):
            val = getattr(self, attr)
            if isinstance(val, str):
                setattr(self, attr, date.fromisoformat(val))


@dataclass
class PaystubLine:
    label: str
    amount: Decimal
    ytd_amount: Decimal
    is_deduction: bool = False


@dataclass
class Paystub:
    id: str
    employee_id: str
    employee_name: str
    pay_period: PayPeriod
    regular_hours: Optional[Decimal]
    overtime_hours: Optional[Decimal]
    gross_pay: Decimal
    federal_income_tax: Decimal
    state_income_tax: Decimal
    ss_tax: Decimal
    medicare_tax: Decimal
    pre_tax_deductions: Decimal
    post_tax_deductions: Decimal
    net_pay: Decimal
    lines: List[PaystubLine]
    ytd_gross: Decimal
    ytd_net: Decimal
    generated_at: datetime = field(default_factory=datetime.utcnow)
    check_number: Optional[str] = None

    def __post_init__(self):
        for attr in ("gross_pay", "federal_income_tax", "state_income_tax",
                     "ss_tax", "medicare_tax", "pre_tax_deductions",
                     "post_tax_deductions", "net_pay", "ytd_gross", "ytd_net"):
            val = getattr(self, attr)
            if isinstance(val, (int, float, str)):
                setattr(self, attr, Decimal(str(val)))
        if self.regular_hours is not None and isinstance(self.regular_hours, (int, float, str)):
            self.regular_hours = Decimal(str(self.regular_hours))
        if isinstance(self.generated_at, str):
            self.generated_at = datetime.fromisoformat(self.generated_at)


@dataclass
class YearEndSummary:
    employee_id: str
    employee_name: str
    year: int
    ytd_gross: Decimal
    ytd_federal_tax: Decimal
    ytd_ss_tax: Decimal
    ytd_medicare_tax: Decimal
    ytd_state_tax: Decimal
    ytd_pre_tax_deductions: Decimal
    ytd_post_tax_deductions: Decimal
    ytd_net: Decimal
    w2_box1: Decimal   # Wages subject to FIT
    w2_box3: Decimal   # SS wages
    w2_box5: Decimal   # Medicare wages


# ─── Database Layer ────────────────────────────────────────────────────────────
class PayrollDB:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self.transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS employees (
                    id              TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    salary          TEXT NOT NULL,
                    hourly_rate     TEXT,
                    pay_frequency   TEXT NOT NULL,
                    filing_status   TEXT NOT NULL DEFAULT 'single',
                    w4_allowances   INTEGER NOT NULL DEFAULT 1,
                    state           TEXT NOT NULL DEFAULT 'CA',
                    status          TEXT NOT NULL DEFAULT 'active',
                    hire_date       TEXT NOT NULL,
                    department      TEXT NOT NULL DEFAULT '',
                    title           TEXT NOT NULL DEFAULT '',
                    email           TEXT NOT NULL DEFAULT '',
                    ytd_gross       TEXT NOT NULL DEFAULT '0',
                    ytd_federal_tax TEXT NOT NULL DEFAULT '0',
                    ytd_ss_tax      TEXT NOT NULL DEFAULT '0',
                    ytd_medicare_tax TEXT NOT NULL DEFAULT '0',
                    ytd_deductions  TEXT NOT NULL DEFAULT '0'
                );

                CREATE TABLE IF NOT EXISTS paystubs (
                    id                  TEXT PRIMARY KEY,
                    employee_id         TEXT NOT NULL REFERENCES employees(id),
                    employee_name       TEXT NOT NULL,
                    period_start        TEXT NOT NULL,
                    period_end          TEXT NOT NULL,
                    pay_date            TEXT NOT NULL,
                    regular_hours       TEXT,
                    overtime_hours      TEXT,
                    gross_pay           TEXT NOT NULL,
                    federal_income_tax  TEXT NOT NULL,
                    state_income_tax    TEXT NOT NULL,
                    ss_tax              TEXT NOT NULL,
                    medicare_tax        TEXT NOT NULL,
                    pre_tax_deductions  TEXT NOT NULL,
                    post_tax_deductions TEXT NOT NULL,
                    net_pay             TEXT NOT NULL,
                    ytd_gross           TEXT NOT NULL,
                    ytd_net             TEXT NOT NULL,
                    check_number        TEXT,
                    generated_at        TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deductions (
                    id              TEXT PRIMARY KEY,
                    employee_id     TEXT NOT NULL REFERENCES employees(id),
                    deduction_type  TEXT NOT NULL,
                    amount          TEXT NOT NULL,
                    description     TEXT NOT NULL,
                    is_percentage   INTEGER NOT NULL DEFAULT 0,
                    active          INTEGER NOT NULL DEFAULT 1,
                    created_at      TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_paystubs_employee
                    ON paystubs(employee_id, pay_date);
                CREATE INDEX IF NOT EXISTS idx_deductions_employee
                    ON deductions(employee_id, active);
            """)

    def save_employee(self, emp: Employee) -> Employee:
        with self.transaction() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO employees
                   (id, name, salary, hourly_rate, pay_frequency, filing_status,
                    w4_allowances, state, status, hire_date, department, title, email,
                    ytd_gross, ytd_federal_tax, ytd_ss_tax, ytd_medicare_tax, ytd_deductions)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    emp.id, emp.name, str(emp.salary),
                    str(emp.hourly_rate) if emp.hourly_rate else None,
                    emp.pay_frequency.value, emp.filing_status,
                    emp.w4_allowances, emp.state, emp.status.value,
                    emp.hire_date.isoformat(), emp.department, emp.title, emp.email,
                    str(emp.ytd_gross), str(emp.ytd_federal_tax),
                    str(emp.ytd_ss_tax), str(emp.ytd_medicare_tax), str(emp.ytd_deductions),
                ),
            )
        return emp

    def get_employee(self, emp_id: str) -> Optional[Employee]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
            return self._row_to_employee(row) if row else None
        finally:
            conn.close()

    def update_ytd(self, emp_id: str, gross: Decimal, federal: Decimal,
                   ss: Decimal, medicare: Decimal, deductions: Decimal):
        with self.transaction() as conn:
            conn.execute(
                """UPDATE employees SET
                   ytd_gross = CAST(CAST(ytd_gross AS REAL) + ? AS TEXT),
                   ytd_federal_tax = CAST(CAST(ytd_federal_tax AS REAL) + ? AS TEXT),
                   ytd_ss_tax = CAST(CAST(ytd_ss_tax AS REAL) + ? AS TEXT),
                   ytd_medicare_tax = CAST(CAST(ytd_medicare_tax AS REAL) + ? AS TEXT),
                   ytd_deductions = CAST(CAST(ytd_deductions AS REAL) + ? AS TEXT)
                   WHERE id=?""",
                (str(gross), str(federal), str(ss), str(medicare), str(deductions), emp_id),
            )

    def save_paystub(self, stub: Paystub):
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO paystubs
                   (id, employee_id, employee_name, period_start, period_end, pay_date,
                    regular_hours, overtime_hours, gross_pay, federal_income_tax,
                    state_income_tax, ss_tax, medicare_tax, pre_tax_deductions,
                    post_tax_deductions, net_pay, ytd_gross, ytd_net, check_number, generated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    stub.id, stub.employee_id, stub.employee_name,
                    stub.pay_period.start_date.isoformat(),
                    stub.pay_period.end_date.isoformat(),
                    stub.pay_period.pay_date.isoformat(),
                    str(stub.regular_hours) if stub.regular_hours else None,
                    str(stub.overtime_hours) if stub.overtime_hours else None,
                    str(stub.gross_pay), str(stub.federal_income_tax),
                    str(stub.state_income_tax), str(stub.ss_tax), str(stub.medicare_tax),
                    str(stub.pre_tax_deductions), str(stub.post_tax_deductions),
                    str(stub.net_pay), str(stub.ytd_gross), str(stub.ytd_net),
                    stub.check_number, stub.generated_at.isoformat(),
                ),
            )

    def get_deductions(self, emp_id: str) -> List[Deduction]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM deductions WHERE employee_id=? AND active=1", (emp_id,)
            ).fetchall()
            return [self._row_to_deduction(r) for r in rows]
        finally:
            conn.close()

    def save_deduction(self, ded: Deduction):
        with self.transaction() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO deductions
                   (id, employee_id, deduction_type, amount, description,
                    is_percentage, active, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    ded.id, ded.employee_id, ded.deduction_type.value,
                    str(ded.amount), ded.description,
                    1 if ded.is_percentage else 0,
                    1 if ded.active else 0,
                    datetime.utcnow().isoformat(),
                ),
            )

    def list_employees(self, status: Optional[EmployeeStatus] = None) -> List[Employee]:
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM employees WHERE status=? ORDER BY name", (status.value,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM employees ORDER BY name").fetchall()
            return [self._row_to_employee(r) for r in rows]
        finally:
            conn.close()

    def get_paystubs(self, emp_id: str, year: Optional[int] = None) -> List[Paystub]:
        conn = self._connect()
        try:
            if year:
                rows = conn.execute(
                    "SELECT * FROM paystubs WHERE employee_id=? AND pay_date LIKE ? ORDER BY pay_date",
                    (emp_id, f"{year}%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM paystubs WHERE employee_id=? ORDER BY pay_date DESC", (emp_id,)
                ).fetchall()
            return [self._row_to_paystub(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _row_to_employee(row: sqlite3.Row) -> Employee:
        return Employee(
            id=row["id"], name=row["name"], salary=Decimal(row["salary"]),
            hourly_rate=Decimal(row["hourly_rate"]) if row["hourly_rate"] else None,
            pay_frequency=PayFrequency(row["pay_frequency"]),
            filing_status=row["filing_status"],
            w4_allowances=row["w4_allowances"],
            state=row["state"],
            status=EmployeeStatus(row["status"]),
            hire_date=date.fromisoformat(row["hire_date"]),
            department=row["department"] or "",
            title=row["title"] or "",
            email=row["email"] or "",
            ytd_gross=Decimal(row["ytd_gross"]),
            ytd_federal_tax=Decimal(row["ytd_federal_tax"]),
            ytd_ss_tax=Decimal(row["ytd_ss_tax"]),
            ytd_medicare_tax=Decimal(row["ytd_medicare_tax"]),
            ytd_deductions=Decimal(row["ytd_deductions"]),
        )

    @staticmethod
    def _row_to_deduction(row: sqlite3.Row) -> Deduction:
        return Deduction(
            id=row["id"], employee_id=row["employee_id"],
            deduction_type=DeductionType(row["deduction_type"]),
            amount=Decimal(row["amount"]), description=row["description"],
            is_percentage=bool(row["is_percentage"]), active=bool(row["active"]),
        )

    @staticmethod
    def _row_to_paystub(row: sqlite3.Row) -> Paystub:
        return Paystub(
            id=row["id"], employee_id=row["employee_id"],
            employee_name=row["employee_name"],
            pay_period=PayPeriod(
                start_date=date.fromisoformat(row["period_start"]),
                end_date=date.fromisoformat(row["period_end"]),
                pay_date=date.fromisoformat(row["pay_date"]),
            ),
            regular_hours=Decimal(row["regular_hours"]) if row["regular_hours"] else None,
            overtime_hours=Decimal(row["overtime_hours"]) if row["overtime_hours"] else None,
            gross_pay=Decimal(row["gross_pay"]),
            federal_income_tax=Decimal(row["federal_income_tax"]),
            state_income_tax=Decimal(row["state_income_tax"]),
            ss_tax=Decimal(row["ss_tax"]),
            medicare_tax=Decimal(row["medicare_tax"]),
            pre_tax_deductions=Decimal(row["pre_tax_deductions"]),
            post_tax_deductions=Decimal(row["post_tax_deductions"]),
            net_pay=Decimal(row["net_pay"]),
            ytd_gross=Decimal(row["ytd_gross"]),
            ytd_net=Decimal(row["ytd_net"]),
            check_number=row["check_number"],
            generated_at=datetime.fromisoformat(row["generated_at"]),
            lines=[],
        )


# ─── Payroll Service ───────────────────────────────────────────────────────────
class PayrollService:
    def __init__(self, db: Optional[PayrollDB] = None):
        self.db = db or PayrollDB()

    def add_employee(
        self,
        name: str,
        salary: Decimal,
        pay_frequency: PayFrequency = PayFrequency.BIWEEKLY,
        filing_status: str = "single",
        w4_allowances: int = 1,
        state: str = "CA",
        department: str = "",
        title: str = "",
        email: str = "",
        hourly_rate: Optional[Decimal] = None,
    ) -> Employee:
        emp = Employee(
            id=f"EMP-{str(uuid.uuid4()).upper()[:8]}",
            name=name,
            salary=Decimal(str(salary)),
            hourly_rate=Decimal(str(hourly_rate)) if hourly_rate else None,
            pay_frequency=pay_frequency,
            filing_status=filing_status,
            w4_allowances=w4_allowances,
            state=state.upper(),
            status=EmployeeStatus.ACTIVE,
            hire_date=date.today(),
            department=department,
            title=title,
            email=email,
        )
        self.db.save_employee(emp)
        logger.info("Employee added: %s (%s)", emp.name, emp.id)
        return emp

    def withhold_taxes(
        self,
        gross: Decimal,
        annual_gross: Decimal,
        filing_status: str = "single",
        w4_allowances: int = 1,
        ytd_ss: Decimal = Decimal("0"),
    ) -> Tuple[Decimal, Decimal, Decimal]:
        """Calculate federal income tax, SS tax, Medicare tax.
        Returns (federal_tax, ss_tax, medicare_tax) per period."""
        # Reduce annual gross by W4 allowances
        allowance_reduction = W4_ALLOWANCE_2024 * w4_allowances
        adj_annual = max(Decimal("0"), annual_gross - allowance_reduction - STANDARD_DEDUCTIONS.get(filing_status, Decimal("14600")))

        brackets = FEDERAL_BRACKETS_MFJ_2024 if filing_status == "married" else FEDERAL_BRACKETS_SINGLE_2024
        annual_tax = Decimal("0")
        for min_inc, max_inc, rate in brackets:
            if adj_annual <= min_inc:
                break
            upper = max_inc if max_inc is not None else adj_annual
            in_bracket = min(adj_annual, upper) - min_inc
            if in_bracket <= 0:
                continue
            annual_tax += (in_bracket * rate).quantize(PRECISION, rounding=ROUND_HALF_UP)

        # Convert annual estimate to per-period (approximate)
        # We use annualized method: period_tax = annual_tax / periods_per_year
        # For accuracy, use the actual bracket on annualized income
        from decimal import Decimal
        federal_period = Decimal("0")
        periods = 26  # biweekly default; caller should use actual
        federal_period = (annual_tax / Decimal("26")).quantize(PRECISION, rounding=ROUND_HALF_UP)

        # Social Security
        remaining_ss_base = max(Decimal("0"), SS_WAGE_BASE - ytd_ss)
        ss_taxable = min(gross, remaining_ss_base)
        ss_tax = (ss_taxable * SS_RATE).quantize(PRECISION, rounding=ROUND_HALF_UP)

        # Medicare
        medicare_tax = (gross * MEDICARE_RATE).quantize(PRECISION, rounding=ROUND_HALF_UP)
        from decimal import Decimal as D
        ytd_with_this = ytd_ss + gross  # approximate
        if ytd_with_this > ADDITIONAL_MEDICARE_THRESHOLD:
            extra = min(gross, ytd_with_this - ADDITIONAL_MEDICARE_THRESHOLD)
            medicare_tax += (extra * ADDITIONAL_MEDICARE_RATE).quantize(PRECISION, rounding=ROUND_HALF_UP)

        return federal_period, ss_tax, medicare_tax

    def calculate_net_pay(
        self,
        employee: Employee,
        hours: Optional[Decimal] = None,
        overtime_hours: Optional[Decimal] = None,
    ) -> Dict:
        """Calculate net pay for a pay period."""
        if employee.status != EmployeeStatus.ACTIVE:
            raise ValueError(f"Employee {employee.id} is not active")

        # Gross pay
        if employee.is_hourly and hours is not None:
            regular = min(hours, Decimal("40"))
            overtime = max(Decimal("0"), hours - Decimal("40")) + (overtime_hours or Decimal("0"))
            gross = (regular * employee.hourly_rate + overtime * employee.hourly_rate * Decimal("1.5")).quantize(PRECISION)
        else:
            gross = employee.period_gross
            regular = None
            overtime = None

        # Deductions
        deductions = self.db.get_deductions(employee.id)
        pre_tax_total = Decimal("0")
        post_tax_total = Decimal("0")

        pre_tax_types = {
            DeductionType.PRE_TAX_401K, DeductionType.PRE_TAX_HSA,
            DeductionType.PRE_TAX_FSA, DeductionType.PRE_TAX_HEALTH,
        }

        for ded in deductions:
            amt = (gross * ded.amount / 100).quantize(PRECISION) if ded.is_percentage else ded.amount
            if ded.deduction_type in pre_tax_types:
                pre_tax_total += amt
            else:
                post_tax_total += amt

        taxable_gross = gross - pre_tax_total
        annual_taxable = taxable_gross * employee.pay_frequency.periods_per_year

        federal_tax, ss_tax, medicare_tax = self.withhold_taxes(
            taxable_gross,
            annual_taxable,
            employee.filing_status,
            employee.w4_allowances,
            employee.ytd_ss_tax,
        )

        # State income tax (simplified flat estimate; real would need per-state tables)
        state_rate = Decimal("0.05")  # 5% default estimate
        state_tax = (taxable_gross * state_rate).quantize(PRECISION, rounding=ROUND_HALF_UP)

        total_taxes = federal_tax + ss_tax + medicare_tax + state_tax
        net = gross - pre_tax_total - total_taxes - post_tax_total

        return {
            "gross": gross,
            "regular_hours": regular,
            "overtime_hours": overtime,
            "pre_tax_deductions": pre_tax_total,
            "taxable_gross": taxable_gross,
            "federal_tax": federal_tax,
            "state_tax": state_tax,
            "ss_tax": ss_tax,
            "medicare_tax": medicare_tax,
            "post_tax_deductions": post_tax_total,
            "total_taxes": total_taxes,
            "net": net,
        }

    def generate_paystub(
        self,
        employee: Employee,
        period: PayPeriod,
        hours: Optional[Decimal] = None,
        overtime_hours: Optional[Decimal] = None,
    ) -> Paystub:
        """Generate a paystub and persist it."""
        calc = self.calculate_net_pay(employee, hours, overtime_hours)
        check_num = f"CHK-{uuid.uuid4().hex[:6].upper()}"

        lines = [
            PaystubLine("Regular Pay", calc["gross"], employee.ytd_gross + calc["gross"]),
        ]
        if calc["regular_hours"]:
            lines[0].label = f"Regular Pay ({calc['regular_hours']}h)"
        if calc.get("overtime_hours") and calc["overtime_hours"] > 0:
            lines.append(PaystubLine(f"Overtime ({calc['overtime_hours']}h)", calc["gross"], employee.ytd_gross))
        lines.append(PaystubLine("Pre-Tax Deductions", calc["pre_tax_deductions"], employee.ytd_deductions, True))
        lines.append(PaystubLine("Federal Income Tax", calc["federal_tax"], employee.ytd_federal_tax + calc["federal_tax"], True))
        lines.append(PaystubLine("Social Security Tax", calc["ss_tax"], employee.ytd_ss_tax + calc["ss_tax"], True))
        lines.append(PaystubLine("Medicare Tax", calc["medicare_tax"], employee.ytd_medicare_tax + calc["medicare_tax"], True))
        lines.append(PaystubLine(f"State Tax ({employee.state})", calc["state_tax"], Decimal("0"), True))

        stub = Paystub(
            id=str(uuid.uuid4()),
            employee_id=employee.id,
            employee_name=employee.name,
            pay_period=period,
            regular_hours=calc["regular_hours"],
            overtime_hours=calc["overtime_hours"],
            gross_pay=calc["gross"],
            federal_income_tax=calc["federal_tax"],
            state_income_tax=calc["state_tax"],
            ss_tax=calc["ss_tax"],
            medicare_tax=calc["medicare_tax"],
            pre_tax_deductions=calc["pre_tax_deductions"],
            post_tax_deductions=calc["post_tax_deductions"],
            net_pay=calc["net"],
            ytd_gross=employee.ytd_gross + calc["gross"],
            ytd_net=employee.ytd_gross,
            check_number=check_num,
            lines=lines,
        )

        self.db.save_paystub(stub)
        self.db.update_ytd(
            employee.id, calc["gross"], calc["federal_tax"],
            calc["ss_tax"], calc["medicare_tax"],
            calc["pre_tax_deductions"] + calc["post_tax_deductions"],
        )
        logger.info("Paystub generated: %s for %s (net=%s)", stub.id[:8], employee.name, calc["net"])
        return stub

    def bulk_process(
        self, employees: List[Employee], period: PayPeriod
    ) -> List[Paystub]:
        """Process payroll for multiple employees."""
        stubs = []
        errors = []
        for emp in employees:
            try:
                stub = self.generate_paystub(emp, period)
                stubs.append(stub)
            except Exception as e:
                errors.append((emp.id, str(e)))
                logger.error("Payroll failed for %s: %s", emp.name, e)
        if errors:
            logger.warning("Bulk process: %d successes, %d failures", len(stubs), len(errors))
        return stubs

    def year_end_summary(self, employee_id: str, year: int) -> YearEndSummary:
        """Generate year-end W2 summary."""
        emp = self.db.get_employee(employee_id)
        if not emp:
            raise ValueError(f"Employee {employee_id} not found")

        stubs = self.db.get_paystubs(employee_id, year)
        ytd_gross = sum(s.gross_pay for s in stubs)
        ytd_fed = sum(s.federal_income_tax for s in stubs)
        ytd_ss = sum(s.ss_tax for s in stubs)
        ytd_med = sum(s.medicare_tax for s in stubs)
        ytd_state = sum(s.state_income_tax for s in stubs)
        ytd_pre = sum(s.pre_tax_deductions for s in stubs)
        ytd_post = sum(s.post_tax_deductions for s in stubs)
        ytd_net = sum(s.net_pay for s in stubs)

        w2_box1 = ytd_gross - ytd_pre  # Wages for federal income tax
        w2_box3 = min(ytd_gross, SS_WAGE_BASE)
        w2_box5 = ytd_gross

        return YearEndSummary(
            employee_id=employee_id,
            employee_name=emp.name,
            year=year,
            ytd_gross=ytd_gross,
            ytd_federal_tax=ytd_fed,
            ytd_ss_tax=ytd_ss,
            ytd_medicare_tax=ytd_med,
            ytd_state_tax=ytd_state,
            ytd_pre_tax_deductions=ytd_pre,
            ytd_post_tax_deductions=ytd_post,
            ytd_net=ytd_net,
            w2_box1=w2_box1,
            w2_box3=w2_box3,
            w2_box5=w2_box5,
        )

    def export_payroll_csv(self, year: int) -> str:
        """Export all paystubs for a year as CSV."""
        employees = self.db.list_employees(EmployeeStatus.ACTIVE)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Employee ID", "Name", "Pay Date", "Gross", "Federal Tax",
            "SS Tax", "Medicare Tax", "State Tax", "Pre-Tax Deductions",
            "Post-Tax Deductions", "Net Pay", "Check #",
        ])
        for emp in employees:
            stubs = self.db.get_paystubs(emp.id, year)
            for s in stubs:
                writer.writerow([
                    s.employee_id, s.employee_name, s.pay_period.pay_date.isoformat(),
                    str(s.gross_pay), str(s.federal_income_tax), str(s.ss_tax),
                    str(s.medicare_tax), str(s.state_income_tax),
                    str(s.pre_tax_deductions), str(s.post_tax_deductions),
                    str(s.net_pay), s.check_number or "",
                ])
        return output.getvalue()


# ─── CLI ───────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="payroll", description="BlackRoad Payroll System")
    parser.add_argument("--db", default=str(DB_PATH))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-employee", help="Add employee")
    p.add_argument("name")
    p.add_argument("salary")
    p.add_argument("--freq", default="biweekly", choices=[f.value for f in PayFrequency])
    p.add_argument("--status-filing", default="single")
    p.add_argument("--allowances", type=int, default=1)
    p.add_argument("--state", default="CA")
    p.add_argument("--dept", default="")
    p.add_argument("--title", default="")

    p = sub.add_parser("run", help="Run payroll for an employee")
    p.add_argument("employee_id")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--paydate", required=True)
    p.add_argument("--hours", default=None)

    p = sub.add_parser("bulk", help="Run payroll for all active employees")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--paydate", required=True)

    p = sub.add_parser("w2", help="Year-end W2 summary")
    p.add_argument("employee_id")
    p.add_argument("--year", type=int, default=datetime.today().year)

    p = sub.add_parser("list", help="List employees")
    p.add_argument("--status", default=None)

    p = sub.add_parser("export", help="Export payroll CSV")
    p.add_argument("--year", type=int, default=datetime.today().year)

    p = sub.add_parser("deduction", help="Add deduction to employee")
    p.add_argument("employee_id")
    p.add_argument("type", choices=[d.value for d in DeductionType])
    p.add_argument("amount")
    p.add_argument("--desc", default="Deduction")
    p.add_argument("--pct", action="store_true")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    svc = PayrollService(PayrollDB(Path(args.db)))

    if args.command == "add-employee":
        emp = svc.add_employee(
            name=args.name, salary=Decimal(args.salary),
            pay_frequency=PayFrequency(args.freq),
            filing_status=args.status_filing,
            w4_allowances=args.allowances,
            state=args.state, department=args.dept, title=args.title,
        )
        print(f"✓ Employee added: {emp.id}")
        print(f"  Name:    {emp.name}")
        print(f"  Salary:  ${emp.salary:,.2f}/yr ({emp.pay_frequency.value})")
        print(f"  Period:  ${emp.period_gross:,.2f}")

    elif args.command == "run":
        emp = svc.db.get_employee(args.employee_id)
        if not emp:
            print(f"Employee {args.employee_id} not found")
            return
        period = PayPeriod(args.start, args.end, args.paydate)
        hours = Decimal(args.hours) if args.hours else None
        stub = svc.generate_paystub(emp, period, hours)
        print(f"\n{'='*55}")
        print(f"  PAYSTUB — {stub.employee_name}")
        print(f"  {stub.pay_period.start_date} – {stub.pay_period.end_date}")
        print(f"  Pay Date: {stub.pay_period.pay_date}  Check: {stub.check_number}")
        print(f"{'─'*55}")
        print(f"  Gross Pay:              ${stub.gross_pay:>10,.2f}")
        print(f"  Pre-Tax Deductions:    -${stub.pre_tax_deductions:>10,.2f}")
        print(f"  Federal Income Tax:    -${stub.federal_income_tax:>10,.2f}")
        print(f"  Social Security Tax:   -${stub.ss_tax:>10,.2f}")
        print(f"  Medicare Tax:          -${stub.medicare_tax:>10,.2f}")
        print(f"  State Tax:             -${stub.state_income_tax:>10,.2f}")
        print(f"  Post-Tax Deductions:   -${stub.post_tax_deductions:>10,.2f}")
        print(f"{'─'*55}")
        print(f"  NET PAY:                ${stub.net_pay:>10,.2f}")
        print(f"{'─'*55}")
        print(f"  YTD Gross:              ${stub.ytd_gross:>10,.2f}")

    elif args.command == "bulk":
        employees = svc.db.list_employees(EmployeeStatus.ACTIVE)
        period = PayPeriod(args.start, args.end, args.paydate)
        stubs = svc.bulk_process(employees, period)
        print(f"✓ Processed {len(stubs)} employees")
        total_net = sum(s.net_pay for s in stubs)
        total_gross = sum(s.gross_pay for s in stubs)
        print(f"  Total Gross: ${total_gross:,.2f}")
        print(f"  Total Net:   ${total_net:,.2f}")

    elif args.command == "w2":
        s = svc.year_end_summary(args.employee_id, args.year)
        print(f"\nW2 Summary — {s.employee_name} — {s.year}")
        print(f"  Box 1 (Wages, FIT):     ${s.w2_box1:>10,.2f}")
        print(f"  Box 2 (Federal W/H):    ${s.ytd_federal_tax:>10,.2f}")
        print(f"  Box 3 (SS Wages):       ${s.w2_box3:>10,.2f}")
        print(f"  Box 4 (SS W/H):         ${s.ytd_ss_tax:>10,.2f}")
        print(f"  Box 5 (Medicare Wages): ${s.w2_box5:>10,.2f}")
        print(f"  Box 6 (Medicare W/H):   ${s.ytd_medicare_tax:>10,.2f}")

    elif args.command == "list":
        status = EmployeeStatus(args.status) if args.status else None
        employees = svc.db.list_employees(status)
        print(f"{'ID':<14} {'Name':<25} {'Salary':>10}  {'Freq':<12} Status")
        print("─" * 75)
        for e in employees:
            print(f"  {e.id:<12} {e.name:<25} ${e.salary:>9,.2f}  {e.pay_frequency.value:<12} {e.status.value}")

    elif args.command == "export":
        print(svc.export_payroll_csv(args.year))

    elif args.command == "deduction":
        ded = Deduction(
            id=str(uuid.uuid4()),
            employee_id=args.employee_id,
            deduction_type=DeductionType(args.type),
            amount=Decimal(args.amount),
            description=args.desc,
            is_percentage=args.pct,
        )
        svc.db.save_deduction(ded)
        pct_str = "%" if args.pct else ""
        print(f"✓ Deduction added: {ded.description} = {args.amount}{pct_str} ({ded.deduction_type.value})")


if __name__ == "__main__":
    main()
