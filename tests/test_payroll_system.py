"""Tests for BlackRoad Payroll System."""

import pytest
from decimal import Decimal
from datetime import date
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from payroll_system import (
    PayrollDB, PayrollService, Employee, PayPeriod, PayFrequency,
    EmployeeStatus, Deduction, DeductionType
)


@pytest.fixture
def svc(tmp_path):
    db = PayrollDB(tmp_path / "payroll.db")
    return PayrollService(db)


PERIOD = PayPeriod("2024-01-01", "2024-01-14", "2024-01-19")


def test_add_employee(svc):
    emp = svc.add_employee("Alice Smith", Decimal("80000"), title="Engineer")
    assert emp.name == "Alice Smith"
    assert emp.salary == Decimal("80000")
    assert emp.status == EmployeeStatus.ACTIVE
    assert emp.id.startswith("EMP-")


def test_employee_period_gross_biweekly(svc):
    emp = svc.add_employee("Bob Jones", Decimal("52000"), pay_frequency=PayFrequency.BIWEEKLY)
    assert emp.period_gross == Decimal("2000.00")  # 52000 / 26


def test_employee_period_gross_monthly(svc):
    emp = svc.add_employee("Carol Lee", Decimal("60000"), pay_frequency=PayFrequency.MONTHLY)
    assert emp.period_gross == Decimal("5000.00")  # 60000 / 12


def test_generate_paystub_salaried(svc):
    emp = svc.add_employee("Dave Kim", Decimal("100000"))
    stub = svc.generate_paystub(emp, PERIOD)
    assert stub.gross_pay == emp.period_gross
    assert stub.net_pay > Decimal("0")
    assert stub.net_pay < stub.gross_pay
    assert stub.federal_income_tax > Decimal("0")
    assert stub.ss_tax > Decimal("0")
    assert stub.medicare_tax > Decimal("0")


def test_generate_paystub_check_number(svc):
    emp = svc.add_employee("Eve Park", Decimal("60000"))
    stub = svc.generate_paystub(emp, PERIOD)
    assert stub.check_number is not None
    assert stub.check_number.startswith("CHK-")


def test_generate_paystub_updates_ytd(svc):
    emp = svc.add_employee("Frank Wu", Decimal("48000"))
    stub = svc.generate_paystub(emp, PERIOD)
    updated_emp = svc.db.get_employee(emp.id)
    assert Decimal(str(updated_emp.ytd_gross)) > Decimal("0")


def test_calculate_net_pay_deductions(svc):
    emp = svc.add_employee("Grace Hall", Decimal("80000"))
    # Add 401k deduction
    ded = Deduction(
        id="ded1", employee_id=emp.id,
        deduction_type=DeductionType.PRE_TAX_401K,
        amount=Decimal("200"), description="401k"
    )
    svc.db.save_deduction(ded)
    result = svc.calculate_net_pay(emp)
    assert result["pre_tax_deductions"] == Decimal("200")
    assert result["taxable_gross"] == result["gross"] - Decimal("200")


def test_withhold_taxes_fica_ss_cap(svc):
    # Employee already near SS wage base
    _, ss, _ = svc.withhold_taxes(
        gross=Decimal("5000"),
        annual_gross=Decimal("168600"),
        ytd_ss=Decimal("165000"),  # nearly at cap
    )
    # SS should be limited to remaining base
    remaining = Decimal("168600") - Decimal("165000")
    expected_ss = (remaining * Decimal("0.062")).quantize(Decimal("0.01"))
    assert ss == expected_ss


def test_withhold_taxes_zero_income(svc):
    fed, ss, med = svc.withhold_taxes(Decimal("0"), Decimal("0"))
    assert fed == Decimal("0")
    assert ss == Decimal("0")
    assert med == Decimal("0")


def test_inactive_employee_paystub_raises(svc):
    emp = svc.add_employee("Henry Ng", Decimal("60000"))
    svc.db.save_employee(Employee(
        **{**emp.__dict__, "status": EmployeeStatus.TERMINATED}
    ))
    terminated = svc.db.get_employee(emp.id)
    with pytest.raises(ValueError, match="not active"):
        svc.calculate_net_pay(terminated)


def test_bulk_process(svc):
    emps = [
        svc.add_employee(f"Employee {i}", Decimal("50000") + Decimal(str(i * 1000)))
        for i in range(5)
    ]
    stubs = svc.bulk_process(emps, PERIOD)
    assert len(stubs) == 5


def test_year_end_summary(svc):
    emp = svc.add_employee("Iris Chen", Decimal("120000"))
    period1 = PayPeriod("2024-01-01", "2024-01-14", "2024-01-19")
    period2 = PayPeriod("2024-01-15", "2024-01-28", "2024-02-02")
    svc.generate_paystub(emp, period1)
    svc.generate_paystub(emp, period2)
    summary = svc.year_end_summary(emp.id, 2024)
    assert summary.year == 2024
    assert summary.ytd_gross > Decimal("0")
    assert summary.w2_box1 > Decimal("0")
    assert summary.w2_box3 <= Decimal("168600")  # capped at SS wage base


def test_export_payroll_csv(svc):
    emp = svc.add_employee("Jack Ma", Decimal("90000"))
    svc.generate_paystub(emp, PERIOD)
    csv_out = svc.export_payroll_csv(2024)
    lines = csv_out.strip().split("\n")
    assert len(lines) >= 2
    assert "Employee ID" in lines[0]
    assert emp.id in csv_out


def test_net_pay_is_positive(svc):
    emp = svc.add_employee("Kate Lin", Decimal("40000"))
    stub = svc.generate_paystub(emp, PERIOD)
    assert stub.net_pay > Decimal("0")


def test_list_employees(svc):
    svc.add_employee("L1", Decimal("50000"))
    svc.add_employee("L2", Decimal("60000"))
    emps = svc.db.list_employees()
    assert len(emps) >= 2
