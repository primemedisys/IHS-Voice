"""Build demo_data/appointments_demo.xlsx for outbound reminder testing."""
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

out_dir = Path("demo_data")
out_dir.mkdir(exist_ok=True)
path = out_dir / "appointments_demo.xlsx"

tomorrow = datetime.now() + timedelta(days=1)

wb = Workbook()
ws = wb.active
ws.title = "Appointments"

headers = ["name", "phone", "date", "time", "language", "consent"]
rows = [
    ["subham pratap", "+917008857139", tomorrow.strftime("%d/%m/%Y"), "10:30 AM", "or", "yes"],
]

ws.append(headers)
for cell in ws[1]:
    cell.font = Font(bold=True)
    cell.fill = PatternFill("solid", fgColor="DDEBF7")
    cell.alignment = Alignment(horizontal="center")

for row in rows:
    ws.append(row)

widths = [22, 18, 14, 12, 10, 10]
for index, width in enumerate(widths, start=1):
    ws.column_dimensions[chr(64 + index)].width = width

wb.save(path)
print("saved:", path.resolve())
print("row 1:", rows[0])
