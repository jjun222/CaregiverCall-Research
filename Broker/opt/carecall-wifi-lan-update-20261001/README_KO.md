# CareCall Pi Wi-Fi LAN 업데이트 — 20261001-lan-1

대상: 기존 `20260929-persist-1`이 설치되어 있고, `wlan0`가 연구실
`192.168.0.0/24`에 연결된 Raspberry Pi 5 / Ubuntu 24.04.
기존 파일과 systemd 서비스의 SHA-256, 정상 연결, 기존 UFW 규칙을 확인한 뒤 적용합니다.

## 바뀌는 기능

저장된 Wi-Fi의 인증·SSID·IPv4 주소·기본 경로를 확인한 후, 해당 wlan0 대역에
TCP 22(SSH), 1883(MQTT)를 허용합니다. 장소를 옮겨 대역이 달라지면 새 규칙 두 개를
먼저 추가하고 이전 대역의 규칙을 제거합니다. 갱신 기록을 디스크에 남겨 중간에
중단되어도 다음 실행에서 이어서 정리합니다.

- 현재 연구실 대역에서는 기존 UFW 규칙 여섯 개를 그대로 사용합니다.
- 설정 AP의 웹·DHCP 규칙 네 개, UFW 기본 정책·다른 규칙·IPv6 규칙을 보존합니다.
- Mosquitto, Avahi, ESP32 펌웨어, AP 비밀번호, 저장된 Wi-Fi 정보를 변경하지 않습니다.
- Wi-Fi 관리자 서비스에 `/etc/ufw` 쓰기 권한을 추가하는 systemd drop-in을 설치합니다.
- 인터넷 접속·Telegram 전달 성공 여부는 이 기능의 LAN 연결 판정에 포함하지 않습니다.

## 지원 범위와 남은 실기 시험

지원 대역은 RFC1918 사설 IPv4(`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)에
완전히 포함된 단일 연결 대역입니다. 실제 DHCP 접두 길이를 사용합니다.
공인 주소, CGNAT(100.64.0.0/10), 링크 로컬, IPv6 전용망, 다중 대역이 모호한 구성은
이번 버전의 자동 허용 대상이 아닙니다. `/31`, `/32`도 제외합니다.
Pi AP `192.168.77.0/24` 또는 ESP32 AP `192.168.78.0/24`와 겹치는 LAN은 제외합니다.
따라서 `192.168.0.0/16` 같은 넓은 LAN도 AP 대역과 겹쳐 사용할 수 없습니다.

Wi-Fi 인증 후 위 조건을 충족하지 못하면 관리자는 온라인 확정을 하지 않고
설정 AP로 돌아가도록 구성되어 있습니다. 기존과 다른 임의의 방화벽 규칙 변경을
발견하면 승인된 규칙으로 간주하지 않고 오류를 보고합니다.

이 패키지의 파일·상태 전환·규칙 갱신·중단 복구 시험은 임시 파일과 모의 UFW/systemd로
실행했으며 31개 시험이 통과했습니다. `systemd-analyze verify` 서비스 문법 검사도
통과했습니다. 실제 Pi에서의 이번 패키지 적용과 다른 공유기 대역에서의 호출·Telegram
왕복 시험은 사용자의 장비에서 확인해야 합니다. Avahi가 실행 중이라는 사실만으로
ESP32의 새로운 Pi 주소 발견 성공까지 보장하지 않습니다.

## 적용 순서

호출·보호자 확인 처리 중이 아닐 때, 현재 연구실 Wi-Fi에 연결된 상태에서 진행합니다.
적용하는 동안 핸드폰에서 Wi-Fi 변경을 요청하거나 별도의 UFW 편집을 하지 마세요.

1. ZIP을 Pi의 `/home/carecallserver/`로 전송합니다.
2. Pi SSH 터미널에서 아래를 실행합니다.

```bash
(
    set -e
    cd /home/carecallserver
    python3 -m zipfile -e CareCall_Pi_WiFi_LAN_20261001.zip .
    cd CareCall_Pi_WiFi_LAN_20261001
    sha256sum -c SHA256SUMS
    sudo python3 -I -B apply_patch.py stage
    sudo python3 -I -B apply_patch.py activate
)
```

`stage`는 기존 앱·설정·UFW 규칙 파일을 Pi 내부의 비공개 디렉터리에 백업하고
새 앱을 준비합니다. 실행 중인 앱과 네트워크는 바꾸지 않습니다. 준비 파일에는
비밀번호 관련 정보가 포함되므로 백업·plan.json을 업로드하지 마세요.

`activate`는 SSH 세션과 별개의 systemd 작업을 예약합니다. Wi-Fi 관리자 재시작으로
SSH가 잠시 끊길 수 있습니다. 원래 공유기·Wi-Fi 설정을 유지하므로 현재 연결 주소로
재접속합니다. 약 30~60초 뒤 재접속을 시도하되 적용 결과가 아직 `ACTIVATING`이면
명령을 반복 적용하지 말고 기다렸다가 상태만 다시 확인합니다.

3. 재접속 후 다음을 실행합니다.

```bash
sudo python3 -I -B /opt/carecall-wifi-lan-update-20261001/apply_patch.py status
sudo python3 -B /opt/carecall-wifi-manager/manager.py status
sudo ufw status numbered
```

성공 조건:

```text
WIFI_LAN_UPDATE_PHASE=COMMITTED
UPDATE_RECOVERY_PENDING=NO
VERSION=20261001-lan-1
PHASE=online
LINK_READY=True
LAN_FAILURE=None
FIREWALL_PRECHECK=PASS
MQTT_NEW_SUBNET_SUPPORT=IMPLEMENTED_PRIVATE_IPV4
LAN_FIREWALL_PHASE=committed
LAN_ALLOWED_SUBNET=192.168.0.0/24
LAN_ALLOWED_PORTS=22,1883
LAN_RULES_MATCH_CURRENT_NETWORK=YES
```

CareCall 서비스 네 개도 모두 `active`여야 합니다. 기존의
`PERSIST_RESULT=COMMITTED` 기록은 Wi-Fi 저장 결과이고, 업데이트 결과는
`WIFI_LAN_UPDATE_PHASE`로 구분합니다. 단순 서비스 재시작은 Wi-Fi 프로필의
`committed_boot_id`를 갱신하지 않습니다.

4. 호출 버튼 → 이번 시각 Telegram 새 알림 → 확인했습니다 → LED 꺼짐을 한 번 확인합니다.
5. 위 결과가 정상인 뒤 Pi 재부팅 후 같은 상태를 확인합니다. 새 공유기 대역 시험은 그다음입니다.

## 실패 또는 중단 시

- 알려진 원본 파일·서비스·방화벽과 다르면 적용 전에 중단합니다. 검사를 지우거나
  `ufw disable`, `ufw allow 1883/tcp` 등의 전체 허용으로 우회하지 마세요.
- 적용 중 정상 연결 확인에 실패하면 기존 관리자로 돌아가고 기존 연구실 허용 대역을
  복구하도록 구현했습니다. `ROLLED_BACK`이면 업데이트 완료가 아닙니다.
- 적용 중 재부팅되면 `carecall-wifi-lan-recover.service`가 Wi-Fi 관리자보다 먼저
  미완료 갱신을 복구합니다. 내구성 있는 `COMMITTED` 기록 이후에는 새 버전을 유지합니다.
- 디스크 오류나 업데이트와 동시에 일어난 설정 변경 등으로 복구 자체가 거부될 수 있습니다.
  상태가 예상과 다르면 상태 명령의 출력만 공유해 주세요. 설치 ZIP·비공개 백업은
  문제 해결 전까지 Pi에 보관합니다.
- 새 공유기에서는 DHCP가 Pi 주소를 다르게 배정할 수 있습니다. 방화벽 갱신이
  이전 IP 주소를 계속 유지시키는 기능은 아닙니다.

## 개발 검증

`tests/`의 UFW/systemd 호출은 모의 구현입니다. 파일 내구성·디렉터리 교환 시험은
임시 디렉터리에서 실제 파일 작업을 합니다. 설치 파일 소유권 검사 때문에 Linux의
격리된 root 시험 환경에서 실행합니다. 사용자 Pi에서 시험 코드를 따로 실행할 필요는 없습니다.

```bash
python3 -B -m unittest discover -s tests -v
```

근거 문서:
- Ubuntu UFW: https://manpages.ubuntu.com/manpages/noble/man8/ufw.8.html
- UFW 파일 구성: https://manpages.ubuntu.com/manpages/noble/man8/ufw-framework.8.html
- systemd 쓰기 허용 경로: https://manpages.ubuntu.com/manpages/noble/man5/systemd.exec.5.html
- SSH와 독립된 예약 실행: https://manpages.ubuntu.com/manpages/noble/man1/systemd-run.1.html
- Mosquitto listener: https://mosquitto.org/man/mosquitto-conf-5.html
