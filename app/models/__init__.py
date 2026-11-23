# Importing every model here lets create_all / Alembic see all tables.
from app.models.call_logs import CallLog
from app.models.call_summary import CallSummary
from app.models.callback import Callback
from app.models.patient import Patient
from app.models.appointment import Appointment
from app.models.inbound_inquiry import InboundInquiry

__all__ = ["Patient", "CallLog", "Callback", "CallSummary", "Appointment", "InboundInquiry"]