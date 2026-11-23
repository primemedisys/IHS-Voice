"""Change the name the reminder call says, in the database and in the demo sheet.

Usage:
    python -m app.scripts.set_patient_name "Subham Nayak"
    python -m app.scripts.set_patient_name "Subham Nayak" --id 1
"""
import argparse
from pathlib import Path

from openpyxl import load_workbook

from app.database import get_session
from app.models.appointment import Appointment

SHEET = Path("demo_data") / "appointments_demo.xlsx"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="the name the bot should say on the phone")
    parser.add_argument("--id", type=int, default=None, help="appointment id (default: every row)")
    args = parser.parse_args()
    name = args.name.strip()
    if not name:
        raise SystemExit("Empty name")

    with get_session() as db:
        query = db.query(Appointment)
        if args.id:
            query = query.filter(Appointment.id == args.id)
        rows = query.all()
        if not rows:
            raise SystemExit("No appointment found")
        for appointment in rows:
            print(f"appointment {appointment.id}: {appointment.patient_name!r} -> {name!r}")
            appointment.patient_name = name

    if SHEET.exists():
        book = load_workbook(SHEET)
        sheet = book.active
        changed = 0
        for row in sheet.iter_rows(min_row=2):
            if row and row[0].value:
                row[0].value = name
                changed += 1
        book.save(SHEET)
        print(f"demo sheet: {changed} row(s) updated in {SHEET}")
    else:
        print(f"demo sheet not found, skipped: {SHEET}")

    print("done")


if __name__ == "__main__":
    main()
