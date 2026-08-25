# 🛰️ Fleet Vision Control Server (Standalone Backend)

Openpilot 경량화 클라이언트와 연동되는 **중앙 관제 및 기기 관리 백엔드 서버**입니다.

---

## 🚀 빠른 시작 (Quick Start)

### 1. 가상환경 생성 및 의존성 설치
```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 서버 실행
```bash
python3 main.py
# 또는 uvicorn 직접 실행:
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

* **웹 관리자 대시보드**: [http://localhost:8000](http://localhost:8000)
* **이벤트 영상 갤러리**: [http://localhost:8000/static/events.html](http://localhost:8000/static/events.html)
* **시스템 로그 & 크래시 뷰어**: [http://localhost:8000/static/logs.html](http://localhost:8000/static/logs.html)
* **Interactive API 문서 (Swagger)**: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## 📁 디렉토리 구조
```
server/
├── README.md              # 서버 실행 안내서
├── server_spec.md         # 상세 서버/API/스트리밍 명세서
├── requirements.txt       # 파이썬 의존성 패키지
├── config.py              # 서버 및 스토리지 설정
├── database.py            # SQLite 데이터베이스 핸들러
├── main.py                # FastAPI 진입점 & WebSocket 허브
├── static/                # 웹 대시보드 UI (Tailwind CSS)
│   ├── index.html         # 메인 대시보드 (차량 그리드 & 1-FPS 썸네일 스트림 & 원격 설정)
│   ├── events.html        # 이벤트 영상 갤러리 및 비디오 플레이어
│   └── logs.html          # 기기 로그 및 크래시 리포트 뷰어
└── storage/               # 업로드된 데이터 및 SQLite DB
    ├── server.db
    ├── videos/
    └── logs/
```

---

## 🔗 독립 Git 저장소로 분리하는 방법
`server` 폴더는 클라이언트 코드와의 의존성이 전혀 없는 **독립된 프로젝트**로 설계되었습니다.
다른 레포지토리로 분리할 때는 다음과 같이 진행하시면 됩니다:

```bash
# 1. 새 디렉토리로 복사
cp -r server/ ~/Development/fleet-control-server

# 2. 새 Git 저장소 초기화
cd ~/Development/fleet-control-server
git init
git add .
git commit -m "feat: Initial commit for Fleet Control Server"
git remote add origin git@github.com:your-org/fleet-control-server.git
git push -u origin main
```
