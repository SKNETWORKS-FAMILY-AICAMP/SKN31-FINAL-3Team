# 관리자용 RunPod 시간제 상시 유지

관리자 환경설정에서 견적서 모델의 Active workers를 1로 유지합니다.
기본 60분, 15~120분 범위. 연장은 클릭 시점부터 선택 시간까지 유지하며
남은 시간을 단축하지 않습니다. Max workers=1, Active workers=0 상태에서만
새 유지 세션을 시작합니다. 이 화면은 GPU 종류/최대 워커 수/메일 정책을 바꾸지 않습니다.

## 종료의 의미와 비용

- 해제는 workersMin=0으로 복귀하는 것입니다. 요청 차단이나 강제 종료가 아닙니다.
- 실행 중 견적서 작업을 취소하지 않습니다. 이후 요청은 기존 Serverless 자동 실행을 사용합니다.
- 실제 종료/유휴 시간과 진행 중 요청에 따라 해제 이후에도 비용이 발생할 수 있습니다.
- 일반 24GB 등급 공개 단가 예시 $0.69/h. 실제 RunPod 계정 단가와 저장 공간 비용은 별도 확인합니다.
- 이 화면에서 켜지 않은 콘솔의 유지 설정은 임의로 인수하지 않습니다.

## 배포 순서

1. 워커 저장소의 `input.warmup=true` 지원 버전을 배포합니다.
2. 기존 배포 절차로 `migrations/015_create_runpod_worker_lease.sql`을 적용합니다.
3. 서버의 RunPod 엔드포인트와 API 키 설정을 확인합니다. 필요하면
   `RUNPOD_CONTROL_API_KEY`에 엔드포인트 관리 권한을 가진 별도 키를 넣습니다.
   브라우저나 Git에는 키를 넣지 않습니다.
4. API 프로세스와 독립적인 자동 해제 타이머를 설치합니다.

```sh
sudo install -m 644 deploy/systemd/biddingflow-runpod-lease.service.example /etc/systemd/system/biddingflow-runpod-lease.service
sudo install -m 644 deploy/systemd/biddingflow-runpod-lease.timer.example /etc/systemd/system/biddingflow-runpod-lease.timer
sudo systemctl daemon-reload
sudo systemctl enable --now biddingflow-runpod-lease.timer
sudo systemctl start biddingflow-runpod-lease.service
```

5. `/etc/biddingflow/backend.env`에 아래를 설정하고 API를 재시작합니다.

```dotenv
RUNPOD_ADMIN_CONTROL_ENABLED=true
RUNPOD_ADMIN_WARMUP_ENABLED=true
```

```sh
sudo systemctl restart biddingflow-api.service
systemctl status biddingflow-runpod-lease.timer
journalctl -u biddingflow-runpod-lease.service --since today
```

6. ERPNext Administrator 또는 System Manager/Purchase Master Manager 계정으로
   관리자 화면에 접근하여 상태를 확인합니다. 시작 버튼을 누르기 전에는 과금 워커를 켜지 않습니다.

## 장애/재시작/동시 조작

종료 시각과 변경자는 PostgreSQL `procurement.runpod_worker_lease`에 저장합니다.
변경 이력은 `runpod_worker_lease_event`에 남깁니다. API 프로세스는 30초마다,
독립 systemd 타이머는 약 1분마다 재확인합니다. 여러 프로세스는 PostgreSQL
세션 advisory lock으로 직렬화합니다. 외부 PATCH 전에 의도를 영구 저장하여
타임아웃이나 프로세스 종료 후에도 만료 해제를 재시도할 수 있습니다.
오래된 브라우저의 변경은 revision 충돌(409)로 거절합니다.

**브라우저 종료에는 영향받지 않지만 AWS 서버 전체/DB/네트워크/RunPod API 장애 시
정시 해제를 보장할 수 없습니다.** 복구 후 재시도합니다. 해당 장애 시 RunPod
콘솔에서 Active workers를 직접 0으로 내리고 과금 상태를 확인해야 합니다.
관리 API 권한은 세션이 모두 해제되기 전까지 철회하지 마세요.

모델 준비는 견적서나 메일을 전송하지 않는 warmup 작업으로 수행합니다.
화면의 준비 완료는 최근 warmup 결과이며, 이후 재배포/워커 교체까지 보장하지 않습니다.
기존 워커 버전이라면 WARMUP 설정을 false로 두면 상시 유지 기능만 사용할 수 있습니다.

롤백 전에는 모든 owned=true 세션을 해제하고 RunPod 콘솔에서 workersMin=0을
확인합니다. 테이블/타이머는 유지해 잔여 만료 작업을 처리하세요.
