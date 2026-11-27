"""Crash-released local locks and recovery using the existing attempt schema."""
from contextlib import contextmanager
import os
from pathlib import Path


class WorkerBusy(RuntimeError):
    pass


@contextmanager
def exclusive_lock(path: Path):
    """Lock a byte, not file existence: the OS releases it after a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    locked = False
    try:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkerBusy('Another JobPilot worker holds this resource; stop it or wait for completion.') from exc
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def recover_attempts(db, timestamp):
    """Caller must own the browser lock, proving no current browser attempt exists."""
    rows = db.execute('SELECT id,job_id,step FROM application_attempts WHERE completed_at IS NULL').fetchall()
    for row in rows:
        submitted = row['step'] == 'submit'
        status = 'ManualReview' if submitted else 'FailedTemporary'
        reason = ('Interrupted after submission started; verify employer receipt before retrying'
                  if submitted else 'Interrupted before submission; safe to retry')
        # A confirmed result survives a crash between confirmation and attempt cleanup.
        job = db.execute('SELECT status FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
        if job and job['status'] == 'Applied':
            status = 'Applied'
        else:
            db.execute('UPDATE jobs SET status=?,eligibility_reason=? WHERE id=?',
                       (status, reason, row['job_id']))
            db.execute('UPDATE application_queue SET status=?,error=? WHERE job_id=?',
                       ('ReviewRequired' if submitted else status, reason, row['job_id']))
        db.execute('UPDATE application_attempts SET status=?,error=?,completed_at=? WHERE id=?',
                   (status, None if status == 'Applied' else reason, timestamp, row['id']))
    db.commit()
    return len(rows)
