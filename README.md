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

모든 긴 작업은 `job_id`를 돌려주고 `C:\DroneJobs\_mcp_jobs\<job_id>\realityscan.log`에 로그를 남긴다.
