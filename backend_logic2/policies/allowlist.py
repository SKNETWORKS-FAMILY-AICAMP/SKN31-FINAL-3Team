"""Administrator edits of the existing dynamic exact-recipient JSON file.

No mail is sent here. Mode/path cannot be changed through this API. PostgreSQL
serializes writers across API processes; atomic replace protects mail readers.
"""
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from pydantic import Field, field_validator
from procurement_db.connection import get_connection
from backend_logic2.integrations.erp_client import get_email_delivery_policy
from .schema import StrictModel


class AllowlistUnavailable(Exception):
    pass


class AllowlistConflict(Exception):
    pass


class SaveAllowlist(StrictModel):
    expected_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    recipients: list[str] = Field(max_length=500)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator('recipients')
    @classmethod
    def exact_addresses_only(cls, values):
        result = []
        for raw in values:
            address = raw.strip().lower()
            if len(address) > 254 or not re.fullmatch(
                r"[a-z0-9!#$%&'+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'+/=?^_`{|}~-]+)*@"
                r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", address
            ):
                raise ValueError('표시명·와일드카드 없이 정확한 이메일 주소만 입력하세요.')
            if address not in result:
                result.append(address)
        return sorted(result)


def _path():
    value = os.getenv('EMAIL_RECIPIENT_ALLOWLIST_PATH', '').strip()
    if not value:
        raise AllowlistUnavailable('파일 기반 화이트리스트가 설정되어 있지 않습니다.')
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise AllowlistUnavailable('화이트리스트 파일 경로를 확인해주세요.')
    return path


def _read(path):
    try:
        if path.stat().st_size > 512_000:
            raise ValueError('file too large')
        raw = path.read_bytes()
        doc = json.loads(raw.decode('utf-8'))
        rows = doc.get('recipients') if isinstance(doc, dict) else doc
        if not isinstance(rows, list) or not all(isinstance(row, str) for row in rows):
            raise ValueError('invalid recipients')
        recipients = SaveAllowlist.exact_addresses_only(rows)
    except (OSError, ValueError, UnicodeError) as exc:
        raise AllowlistUnavailable('화이트리스트 파일을 읽을 수 없거나 형식이 올바르지 않습니다.') from exc
    return raw, doc, recipients


def get_allowlist():
    path = _path()
    raw, doc, recipients = _read(path)
    return {
        'revision': hashlib.sha256(raw).hexdigest(), 'recipients': recipients,
        'delivery_mode': get_email_delivery_policy(),
        'enabled': not isinstance(doc, dict) or doc.get('enabled') is not False,
        'editable': os.access(path.parent, os.W_OK),
    }


def save_allowlist(body: SaveAllowlist, actor: str):
    if len(body.reason.strip()) < 3:
        raise ValueError('변경 사유를 3자 이상 입력하세요.')
    path = _path()
    # This endpoint cannot silently turn on real delivery or unblock a disabled
    # file. Operations must enable custom_only deliberately outside the UI.
    if get_email_delivery_policy() != 'custom_only':
        raise AllowlistUnavailable('custom_only 모드에서만 화이트리스트를 편집할 수 있습니다.')
    with get_connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (310031016,))
        raw, doc, _ = _read(path)
        if hashlib.sha256(raw).hexdigest() != body.expected_revision:
            raise AllowlistConflict('화이트리스트가 변경되었습니다. 다시 불러온 뒤 수정해주세요.')
        if isinstance(doc, dict) and doc.get('enabled') is False:
            raise AllowlistUnavailable('현재 파일의 발송 허용이 비활성화되어 있습니다. 운영 설정을 확인해주세요.')
        updated = dict(doc) if isinstance(doc, dict) else {'enabled': True}
        updated['recipients'] = body.recipients
        updated['updated_by'] = actor
        updated['updated_at'] = datetime.now(timezone.utc).isoformat()
        updated['change_reason'] = body.reason.strip()
        tmp_name = None
        try:
            history = path.parent / 'allowlist-history'
            history.mkdir(mode=0o700, exist_ok=True)
            # Archive BEFORE replacement. A failed backup must prevent a save.
            backup = history / f'{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex}.json'
            with backup.open('xb') as handle:
                os.chmod(backup, 0o600)
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            fd, tmp_name = tempfile.mkstemp(prefix='.allowlist-', dir=path.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            tmp_name = None
        except OSError as exc:
            raise AllowlistUnavailable('화이트리스트 저장 권한 또는 백업 경로를 확인해주세요.') from exc
        finally:
            if tmp_name:
                Path(tmp_name).unlink(missing_ok=True)
    return get_allowlist()
