"""Seed (or update) one patient row so the "existing patient" inbound path works.

Usage (PowerShell):
    python -m app.scripts.seed_test_patients --name "Subham Pratap" --phone "+917008857139" --dob 1995-08-15

Run it again with new values to update the same row (matched by phone number).
"""
import argparse
from datetime import date

from sqlalchemy import select

from app.database import get_session
from app.models.patient import Patient
from app.services.shared.patients import normalize_phone


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="full registered name, e.g. 'Subham Pratap'")
    parser.add_argument("--phone", required=True, help="caller number, e.g. +917008857139")
    parser.add_argument("--dob", required=True, help="date of birth, YYYY-MM-DD")
    args = parser.parse_args()

    phone = normalize_phone(args.phone)
    dob = date.fromisoformat(args.dob)
    name = " ".join(args.name.split())
    if not name:
        raise SystemExit("Empty name")

    with get_session() as db:
        patient = db.scalar(select(Patient).where(Patient.phone_number == phone))
        if patient:
            patient.full_name = name
            patient.date_of_birth = dob
            db.flush()
            print(f"updated patient id={patient.id}: {patient.full_name} | {patient.phone_number} | {patient.date_of_birth}")
        else:
            patient = Patient(full_name=name, phone_number=phone, date_of_birth=dob)
            db.add(patient)
            db.flush()
            print(f"created patient id={patient.id}: {patient.full_name} | {patient.phone_number} | {patient.date_of_birth}")


if __name__ == "__main__":
    main()