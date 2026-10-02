#!/usr/bin/env python3
"""CareCall ESP32 Wi-Fi 사전 점검 — Python 표준 라이브러리만 사용.

Windows에서 실행:
  python -I -B carecall_esp32_wifi_precheck.py --project C:\\Users\\dsc-nb02\\carecall_esp32

프로젝트/빌드 파일을 읽기만 합니다. 빌드, 플래시, COM 접속, 네트워크 접속,
설정 변경, NVS 삭제를 실행하지 않습니다. Wi-Fi 이름/비밀번호, MQTT 주소/
계정/비밀번호, 장치 ID는 출력하지 않습니다. 결과는 현재 PC 파일에 대한
점검이며, 장치에 실제 기록된 펌웨어나 NVS 내용과 같다는 보장은 아닙니다.
"""

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

VERSION = "20260930-esp32-wifi-precheck-2"
REFERENCE_COMMIT = "271bd0e8870aa3a0a26377a8d716394c17a24fba"
EXPECTED = {
    "CMakeLists.txt": "bf20ad5835a6628920a4e007e756a636b23736243fabea9c7320efaf90a62dbe",
    "main/CMakeLists.txt": "3cd7cea94556e63838045fa83101c03c911dc1eadd135c40f7b589316dc4d0f3",
    "main/Kconfig.projbuild": "7be1ba3e9197e9ae9d06de5ccbcf7f3c2d45fd6f6a60a4bf50cce5e49322ae99",
    "main/app_main.cpp": "a50e43c21b723211ea73d3fe6dc7c4a3cca594fbfe96ecde9ccbce5487ee32ff",
    "main/button_driver.cpp": "a06416fef111d7b8f3881871c1ed878906017442cb141f9e39fb07b8778a1dc1",
    "main/button_driver.h": "dfdcdb5861376ca3363457fab32b33407462968e3cdae1957c73eae391e8324d",
    "main/call_led.cpp": "3de19960ce59c4ad9a76f2452e9c8c853b64a8ce1653c4b7bb399a97465f4e55",
    "main/call_led.h": "c3347597ce374f2aa7d83825cebf721fe5f2c0e9697518a4a56b5d5aa8f6f797",
    "main/call_manager.cpp": "247c7ee520d2a99de6289d3738f4d615e5ede59ac38fb3bbb4f9c79118f5f8f8",
    "main/call_manager.h": "f817084e17f1d70fa3573798a5e5bfbd67a25f8d2546a29f01de58b51590a0a0",
    "main/call_outbox.cpp": "2fd3ec2702c146b6ac3c6f40c533992d581b52fa87e59368e30afe6add551d32",
    "main/call_outbox.h": "264d4d536b4c93ce46d7dd0ec4cce9626f300b4b9f996b9e215117c4ffded43b",
    "main/idf_component.yml": "3ded44a96723fce547a1d85d9e6cfaf1053b4ab73ba10ace85b782509f095ed2",
    "main/mqtt_manager.cpp": "2c7047f34cf7c1860836bba83816b889aa3ee0b4e96b05994616ebbf7790b692",
    "main/mqtt_manager.h": "86b4ab48536fa85b593c3f8b6b93f88b0b7344b227808aa10a81eb4ab1d9bccb",
    "main/wifi_manager.cpp": "496c2f87f3783f7ec64dee139250a3d72c1b611a340d6dbf913056aa29befb6f",
    "main/wifi_manager.h": "2c03ff84461cdb041badceea094d63e469e3ada7c873e6a7e84313da25d664a6",
}

# Derived from the same pinned public source, never from private user settings.
EXPECTED_LAYOUT = {
    "CMakeLists.txt": [
        "fdfc43af45b09ab8c4823164126bab363f83403adb4b38c4f6ae080a2eb3c28a",
        "fdfc43af45b09ab8c4823164126bab363f83403adb4b38c4f6ae080a2eb3c28a"
    ],
    "main/CMakeLists.txt": [
        "76e1703c283f49db89d50c68950275d004123e71f2fb4932038a1699a649306d",
        "4e473db3878145dc3d781dd1b9f5c7d9bd7d9d36b7a36499ff219e611c01943d"
    ],
    "main/Kconfig.projbuild": [
        "5f48c32b37f399648f78b6f0fc9489a19783aa993182cf863a8141afbd09d5f4",
        "55663e353c9b157f7efc40814e30136c57589ee891e9de04be50c70cb50a8b1c"
    ],
    "main/app_main.cpp": [
        "6ebe82f655856ab9da9789a3a51b944d87c49484f0f13d9c142fae11f73a9f7f",
        "9346c79effb2321ab070af5277f72b840295987fdcba057795eb9a8c558dc1de"
    ],
    "main/button_driver.cpp": [
        "5353b64b5e7cbc7302a6f9e1d58c2bb58a47b1b52a61cce79003ff1bf6fb4ac2",
        "903a75ff6659b267bade56e9e971fc7921bf86f148566e061ca89019815e91b1"
    ],
    "main/button_driver.h": [
        "1b1554e1b39297dbdd4af2e25ce34e2fe04417a7f64ee3be6c7e41b736b1be41",
        "4779fd30565f6cbccc5676aaede425f2fd6dee29c860e261df941ec275340287"
    ],
    "main/call_led.cpp": [
        "38aa219e3d40ca2d0375b66f174b8052dcf794458fc3a6004d47e8fc93634623",
        "13303cd5166b53c02f9a1df5047f6f9a3294eb9a9becb68181bd36009b273faa"
    ],
    "main/call_led.h": [
        "51d1da85f82fb66b00ea6abca9945efe91eb603273c8a53b03159b529ca489e2",
        "51d1da85f82fb66b00ea6abca9945efe91eb603273c8a53b03159b529ca489e2"
    ],
    "main/call_manager.cpp": [
        "61160d74cb1265f1b076f4bbb8022664e0a940b840c5cc9b6935c40012d1b1cf",
        "6ca5717b7d139e8510d5247b865d903109b2af16f5ccc2dcea0e06a03cef2c88"
    ],
    "main/call_manager.h": [
        "71be188935800e828f59870fd738394837bc18dc500e4c6ed165aabb32ad75bf",
        "bd6946ef4cddc99c4f40d9fea65dda72d2735900106b1303310a1813017a763a"
    ],
    "main/call_outbox.cpp": [
        "6668085d1eea6a11e4ac3519cfc30d56e8727f72cce46993b3df281569ca4d76",
        "b85fd24a48d206b93aa6d85b73cc1b498c35d64fcef8623a4ae06f01ad6f52ce"
    ],
    "main/call_outbox.h": [
        "acc7649e0de07d1a892327c79a16beaa1065fc341dd04513164da9a8dc04bb92",
        "dd5fec353b18d5749b41aaec798be9cf57c79d7036512961fa4a0b8d32b7214a"
    ],
    "main/idf_component.yml": [
        "54437c432c7525cb09a0c53cf6adffe98040925f67260fe501e28f8e5bca5faa",
        "273a556a77d2082cb3aeef5ddd1084647a9af111a17a99e394a7121ed4a6c13a"
    ],
