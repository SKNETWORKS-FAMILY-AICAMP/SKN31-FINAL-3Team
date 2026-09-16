"""Independent systemd safety timer; never starts a new administrator lease."""
import sys
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / '.env')

from backend_logic2.services.runpod_worker_control import reconcile_all, ControlConflict

if __name__ == '__main__':
    try:
        reconcile_all()
    except ControlConflict:
        pass
    except Exception:
        print('Worker lease safety check failed; check database connectivity and RunPod management permission.', file=sys.stderr)
        raise SystemExit(1)
