from backend.database.db import session_scope
from backend.database.models import Alert

with session_scope() as s:
    alerts = s.query(Alert).all()
    print("Alerts count:", len(alerts))
    for a in alerts:
        print(f"ID: {a.id}, Image: {a.image_path}, Timestamp: {a.timestamp}")
