[README_KO.md](https://github.com/user-attachments/files/33137174/README_KO.md)# CareCall Pi 장치·세트 등록 1단계 — 2026-10-07

이 패키지는 N개 호출 버튼 확장의 **등록 구조**를 추가합니다.
추가 버튼의 MQTT 호출 수신이나 세트 단위 보호자 확인은 아직 활성화하지 않습니다.
현재 button01의 호출·Telegram 확인 기능은 기존 프로그램이 계속 처리합니다.

## 변경 내용

- `device_registry.py`, `device_admin.py` 두 파일을 Pi 수신기 폴더에 새로 추가합니다.
- 운영 SQLite DB에 `carecall_registry_meta`, `carecall_sets`, `carecall_devices` 세 테이블을 추가합니다.
- 첫 세트 `set01`과 기존 장치 `button01`을 연결합니다.
- `button01` 상태는 `legacy`입니다. 기존 단일 버튼 프로그램이 처리한다는 의미입니다.
- 추가 장치는 `staged`로만 등록할 수 있습니다. 등록만으로 사용 가능한 버튼이 되지 않습니다.
- 등록 정보에는 Wi-Fi/MQTT 비밀번호, Telegram 토큰, 보호자 개인정보를 넣지 않습니다.
- 기존 파일·기존 테이블·기존 `PRAGMA user_version`은 변경하지 않습니다.
- 서비스 재시작, Mosquitto 계정/ACL 변경, ESP32 업로드는 수행하지 않습니다.

## 적용 전 확인과 백업

`--check`는 패키지 해시, 2026-10-06에 내려받은 Pi 소스 24개 파일의 해시,
기존 DB의 필수 테이블/열, button01 이외의 기존 데이터 여부, DB quick_check를 확인합니다.
소스가 달라졌거나 예상 밖 DB 상태면 중단합니다. 파일을 강제로 되돌리거나 검사를 해제하지 마세요.

`--apply`는 같은 검사를 수행하고, SQLite Online Backup API로 운영 DB의 백업을 만든 다음
새 코드 파일을 추가하고 BEGIN IMMEDIATE 트랜잭션으로 등록 테이블을 생성합니다.
다른 프로세스가 쓰기를 진행 중이면 최대 5초 기다린 뒤 잠금 획득 실패 시 중단할 수 있습니다.
백업은 최대 약 30초 안에 끝나지 않으면 중단합니다.
전체 DB를 자동 복원하지 않습니다. 백업 이후 접수된 호출을 잃지 않기 위한 조치입니다.

백업 기본 위치: `/home/carecallserver/CareCall_Pi_Backups/registry_20261007_<임의문자>/`
백업에는 기존 운영 DB의 개인정보가 들어갈 수 있으므로 GitHub나 채팅에 올리지 마세요.
백업 폴더는 0700, DB 백업 파일은 0600 권한으로 생성합니다.

## Windows에서 준비

ZIP을 풀어 다음 파일이 바로 보이도록 배치합니다.

`C:\Users\dsc-nb02\Carecall_main_file\CareCall_Pi_Registry_20261007\apply_registry.py`

Windows PowerShell:

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $carecallPackage = 'C:\Users\dsc-nb02\Carecall_main_file\CareCall_Pi_Registry_20261007'
    if (-not (Test-Path -LiteralPath (Join-Path $carecallPackage 'apply_registry.py') -PathType Leaf)) {
        throw '압축 해제 위치를 확인하세요.'
    }
    Get-Command scp -ErrorAction Stop | Out-Null
    & scp -r $carecallPackage 'carecallserver@192.168.0.7:/home/carecallserver/'
    if ($LASTEXITCODE -ne 0) { throw 'Pi 파일 전송에 실패했습니다.' }
    Write-Output 'PI_PACKAGE_TRANSFER=PASS'
}
```

192.168.0.7은 마지막으로 확인한 Pi 주소입니다. 바뀌었다면 확인한 현재 주소로 수정합니다.

## Pi에서 적용

기존 Pi SSH 터미널에서 carecallserver 계정으로 실행합니다. **sudo를 붙이지 않습니다.**

```bash
(
    set -e
    cd /home/carecallserver/CareCall_Pi_Registry_20261007
    python3 -B apply_registry.py --check
    python3 -B apply_registry.py --apply --set-id set01
    python3 -B /home/carecallserver/carecall_receiver/device_admin.py status
    systemctl show carecall-receiver.service carecall-telegram.service carecall-registration.service mosquitto.service --property=Id,ActiveState --no-pager
)
```

성공 출력의 핵심:

```text
PACKAGE_HASHES=PASS
PI_BASELINE_SOURCE_HASHES=PASS
DATABASE_CHECK=PASS
REGISTRY_CHECK=PASS
DATABASE_BACKUP=PASS
REGISTRY_APPLY=PASS
LEGACY_DEVICE=button01
LEGACY_SET=set01
ADDITIONAL_BUTTON_CALLS_ENABLED=NO
SERVICE_RESTART_REQUESTED=NO
```

동일한 버전을 다시 적용하면 `REGISTRY_APPLY=ALREADY_INSTALLED`로 끝납니다.
`additional_button_calls_enabled: false`는 이 단계의 정상 상태입니다.
적용 후 기존 버튼 1회의 호출 → Telegram 알림 → 보호자 확인 → LED 소등을 확인합니다.

## 추가 장치 예약 등록 (필요할 때)

```bash
python3 -B /home/carecallserver/carecall_receiver/device_admin.py stage-device --device-id button02 --set-id set01
```

이 명령은 장치를 `staged`로 등록할 뿐, MQTT 계정이나 비밀번호를 만들지 않습니다.
같은 장치를 다시 등록하면 그대로 유지하고, 다른 세트로 바꾸려 하면 중단합니다.
버튼 5개를 등록하더라도 N개 호출 기능은 다음 단계까지 완료해야 동작합니다.

## GitHub 수작업 커밋 대상

| 패키지 파일 | 저장소 경로 |
|---|---|
| app/device_registry.py | Broker/home/carecallserver/carecall_receiver/device_registry.py |
| app/device_admin.py | Broker/home/carecallserver/carecall_receiver/device_admin.py |
| 패키지 전체(원본 디렉터리 구조 유지) | Broker/tools/CareCall_Pi_Registry_20261007/ |

패키지 디렉터리는 설치·테스트·기준 해시를 함께 보존하기 위한 것입니다.
운영 DB, 설치 후 만들어진 백업, 로컬 설정·비밀번호 파일은 커밋 대상이 아닙니다.

## 테스트

패키지 디렉터리에서 다음 명령을 실행하면 임시 DB만 사용하는 테스트를 수행합니다.

```bash
python3 -B -m unittest discover -s tests -v
```

기존 DB 구조·호출·보호자·운영자 정보 보존, 재실행, 20개 등록, 중복 ID,
세트 변경 거부, 잘못된 입력, 부분 스키마, 트랜잭션 실패 롤백을 확인합니다.
실제 Pi 서비스와 ESP32 하드웨어 동작은 사용자의 환경에서 확인해야 합니다.

## 다음 단계

등록 정보를 실제 수신 검증과 알림 수신자 연결에 사용하고, 최신 알림의 유효성을 세트
기준으로 관리하도록 변경합니다. 보호자 확인 시점에 Pi에 저장된 같은 세트의 대기 호출을
대상으로 장치별 확인 응답을 보내며, 이후 호출과 아직 도착하지 않은 오프라인 호출은 유지합니다.
ESP32에는 공통 코드와 장치별 설정을 분리하는 변경을 파일별 전체 코드로 제공합니다.

공식 근거:
- https://www.sqlite.org/backup.html
- https://www.sqlite.org/lang_transaction.html
- https://docs.python.org/3/library/sqlite3.html

