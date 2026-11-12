"""QueuePilot audit log, independent from upstream telephone vendors."""
import time

def audit(db, actor: str, operation: str, entity_id: str, details: str = '') -> None:
    db.execute('''INSERT INTO audit_log(at,actor,operation,entity_id,details)
                  VALUES(?,?,?,?,?)''', (time.time(), actor, operation, entity_id, details))
