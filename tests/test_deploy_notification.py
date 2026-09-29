"""Validate the actual workflow and message branches without posting to Discord."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/deploy.yml'
DATA = yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))
NOTIFY = DATA['jobs']['notify']
STEP = NOTIFY['steps'][0]


def _bash():
    if os.name == 'nt':
        candidate = Path('C:/Program Files/Git/bin/bash.exe')
        if candidate.is_file():
            return str(candidate)
    elif shutil.which('bash'):
        return shutil.which('bash')
    pytest.skip('Bash unavailable; Linux CI runs these shell contract checks')


def test_notifications_are_not_inside_the_deployment_gate():
    assert NOTIFY['needs'] == ['deploy']
    assert 'always()' in NOTIFY['if']
    assert "workflow_run.event == 'push'" in NOTIFY['if']
    assert "workflow_run.head_branch == 'main'" in NOTIFY['if']
    assert 'conclusion' not in NOTIFY['if']
    assert "conclusion == 'success'" in DATA['jobs']['deploy']['if']
    assert NOTIFY['environment']['name'] == 'production'
    assert STEP['env']['DEPLOY_JOB_STATUS'] == '${{ needs.deploy.result }}'
    assert not STEP.get('continue-on-error', False)
    assert all(s['name'] != 'Notify Discord' for s in DATA['jobs']['deploy']['steps'])
    # workflow_run from PRs must not execute checked-out untrusted code with secrets.
    assert all('uses' not in s for s in NOTIFY['steps'])


def test_notification_shell_has_valid_syntax():
    subprocess.run([_bash(), '-n'], input=STEP['run'], text=True, encoding='utf-8', check=True)


@pytest.mark.parametrize(('ci', 'deploy', 'expected', 'link'), [
    ('failure', 'skipped', 'CI 미통과 · 배포하지 않음', 'ci-url'),
    ('cancelled', 'skipped', 'CI 미통과 · 배포하지 않음', 'ci-url'),
    ('timed_out', 'skipped', 'CI 미통과 · 배포하지 않음', 'ci-url'),
    ('success', 'success', '배포 완료', 'cd-url'),
    ('success', 'failure', '배포 실패', 'cd-url'),
    ('success', 'cancelled', '배포 중단', 'cd-url'),
    ('success', 'skipped', '배포 중단', 'cd-url'),
    ('', 'success', '배포 완료', 'cd-url'),  # manual dispatch
])
def test_actual_message_branch(ci, deploy, expected, link):
    script = STEP['run']
    start = script.index('if [[ -n "$CI_RESULT"')
    end = script.index('\npayload=', start)
    # Run only message selection. No curl, credentials or remote services.
    result = subprocess.run([_bash(), '-c', script[start:end] + '\nprintf "%s\\n" "$title" "$description" "$run_url"'],
                            env={**os.environ, 'CI_RESULT': ci, 'DEPLOY_JOB_STATUS': deploy,
                                 'CI_RUN_URL': 'ci-url', 'run_url': 'cd-url'},
                            capture_output=True, text=True, encoding='utf-8', check=True)
    assert expected in result.stdout
    assert result.stdout.strip().endswith(link)
    if deploy == 'success' and ci in {'', 'success'}:
        assert 'pull' in result.stdout
    else:
        assert '최신 main이 반영되었습니다' not in result.stdout
