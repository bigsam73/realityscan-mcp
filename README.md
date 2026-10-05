# RealityScan MCP

Claude Code에서 Epic RealityScan(구 RealityCapture)을 직접 제어하는 MCP 서버.

- 실행: `uv --directory C:\Users\Administrator\RealityScan-MCP run server.py`
- 등록: `claude mcp add --scope user realityscan -- C:\Users\Administrator\.local\bin\uv.exe --directory C:\Users\Administrator\RealityScan-MCP run server.py`
- 환경변수: `REALITYSCAN_EXE`(실행 파일 경로, 자동 탐지됨), `RS_JOBS_DIR`(작업 로그 루트, 기본 `C:\DroneJobs`)
- 테스트: `uv run test_client.py`

## 도구

| 도구 | 역할 |
| --- | --- |
| rs_status | 설치 여부, GPU, 실행 중인 인스턴스, 작업 목록 |
| rs_cli_reference | CLI 명령 요약과 문서 링크 |
| rs_inspect_images | 사진 폴더 점검(장수, 해상도, GPS 유무)과 처리 제안 |
| rs_drone_pipeline | 정렬→메쉬→텍스처→3D Tiles/GLB 내보내기 한 번에 (백그라운드 작업) |
| rs_align / rs_mesh / rs_texture / rs_export | 단계별 실행 |
| rs_run | 임의 CLI 명령 시퀀스 실행 |
| rs_job_status / rs_job_list / rs_job_abort | 작업 상태·로그 확인, 중단 |
| rs_gui_launch / rs_gui_send / rs_gui_status / rs_gui_abort | 보이는 RealityScan 창을 띄우고 명령 위임 |
| rs_tiled_pipeline | 3만 장급 대용량 사진을 GPS 격자 타일로 나눠 순차 처리, 부모 tileset.json으로 병합 |
| rs_tiled_status / rs_tiled_abort / rs_tiled_merge | 타일 진행 상태, 중단(이어하기 가능), 부모 tileset 재생성 |

모든 긴 작업은 `job_id`를 돌려주고 `C:\DroneJobs\_mcp_jobs\<job_id>\realityscan.log`에 로그를 남긴다.

## 대용량(타일) 파이프라인

사진이 수천 장을 넘으면 한 프로젝트로는 RAM 16 GB에서 정렬이 불가능하다. `rs_tiled_pipeline`은
촬영 순서가 아니라 **GPS 위치 기준 정사각 격자**로 나눈다 (`tiling.py`).

1. 모든 사진의 EXIF GPS를 읽어 `gps_index.csv`로 캐시 (EXIF가 없으면 `gps_csv`: `name,lat,lon[,alt]`)
2. WGS84 → UTM(m) 투영 후, **버퍼(기본 20 %)를 포함한 사진 수가 `max_photos_per_tile`(기본 500) 이하**가 되는 가장 큰 칸 크기를 이분 탐색
3. 타일마다 버퍼 범위의 사진을 `images/<tile>/`에 하드링크(같은 볼륨, 용량 0)하고 `-addFolder`
4. 정렬 후 `-setReconstructionRegion regions/<tile>.rcbox`로 **칸 안쪽만** 메쉬 → 이웃 타일과 겹치지 않음
   (`region_mode="auto"`면 RealityScan 자동 영역, 타일끼리 겹침)
5. 타일별 `tiles/<tile>/tileset.json` 내보내기, 끝나면 부모 `tiles/tileset.json`이 전부 참조 (Cesium에서 한 장의 맵)

```
rs_tiled_pipeline(images_folder, output_folder, name, max_photos_per_tile=500, dry_run=True)  # 격자 계획만 확인
rs_tiled_pipeline(..., max_tiles=1)        # 타일 1개만 먼저 돌려 검증
rs_tiled_pipeline(...)                     # 같은 name으로 다시 부르면 이어서 실행 (resume)
rs_tiled_pipeline(..., retry_failed=True)  # 실패 타일 재시도
rs_tiled_status(run_dir) / rs_tiled_abort(run_dir) / rs_tiled_merge(run_dir)
```

상태는 `<output_folder>/<name>/plan.json`에 저장되므로 서버를 재시작해도 이어서 할 수 있다.
타일 수 × 시간이 길어지므로(3만 장 ≈ 60 ~ 70 타일, 타일당 1 ~ 2시간) `max_tiles`로 끊어 실행하는 것을 권장한다.

**검증 필요**: `.rcbox` 파일 포맷(UTM proj 문자열 + 중심/크기)은 RealityScan 설치 후 첫 타일에서
`regions/<tile>_auto.rcbox`(RealityScan이 직접 내보낸 파일)와 비교해 확인할 것. 맞지 않으면
`region_mode="auto"`로 돌리거나 템플릿(`tiling.RCBOX_TEMPLATE`)을 수정한다.

테스트: `uv run test_tiling.py` (합성 GPS 사진 1,200장 + 가짜 RealityScan.exe로 전체 흐름 검증)
