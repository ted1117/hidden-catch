# HiddenCatch Backend

FastAPI 기반으로 구현된 HiddenCatch 서비스의 백엔드.

## 기술 스택

- **Python 3.13+**
- **FastAPI** + **Uvicorn** - 웹 프레임워크 및 ASGI 서버
- **SQLAlchemy 2.0** - ORM
- **PostgreSQL** - 데이터베이스
- **Alembic** - 데이터베이스 마이그레이션
- **Celery** - 비동기 작업 큐
- **Redis** - Celery 브로커 및 결과 백엔드
- **AWS S3** - 이미지 파일 저장
- **Google Cloud Vision API** - 이미지 분석 및 차이점 탐지
- **Ruff** - 포맷터 및 린터

## 프로젝트 구조

```
src/backend
├── app/
│   ├── api/
│   │   └── v1/
│   │       ├── endpoints/
│   │       │   └── games.py      # 게임 관련 API 엔드포인트
│   │       └── router.py         # API 라우터 설정
│   ├── core/
│   │   └── config.py             # 설정 관리
│   ├── db/
│   │   ├── base.py               # SQLAlchemy Base
│   │   ├── session.py            # DB 세션 관리
│   │   ├── init_db.py            # DB 초기화
│   │   └── utils.py              # DB 유틸리티
│   ├── models/
│   │   ├── game.py               # 게임 관련 모델
│   │   ├── puzzle.py             # 퍼즐 관련 모델
│   │   └── upload_slot.py        # 업로드 슬롯 모델
│   ├── schemas/
│   │   ├── game.py               # 게임 관련 스키마
│   │   ├── puzzle.py             # 퍼즐 관련 스키마
│   │   └── types.py              # 공통 타입
│   ├── services/
│   │   └── game_service.py       # 게임 비즈니스 로직
│   ├── worker/
│   │   ├── celery_app.py         # Celery 앱 설정
│   │   ├── tasks.py              # Celery 작업 정의
│   │   └── detect.py             # 이미지 차이점 탐지 로직
│   └── main.py                   # FastAPI 앱 진입점
├── migrations/                   # Alembic 마이그레이션 파일
├── pyproject.toml               # 프로젝트 설정 및 의존성
└── README.md
```

## 시작하기

### 1. 의존성 설치

```bash
uv sync
```

### 2. 환경 변수 설정

`.env` 파일을 생성하고 아래 환경 변수들을 설정합니다:

```env
# 애플리케이션 설정
DEBUG=True
ENVIRONMENT=development
ALLOWED_ORIGINS=["http://localhost:3000"]

# 데이터베이스 설정
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/hiddencatch
# 또는 AWS RDS 터널 사용 시
AWS_RDS_URL_TUNNEL=postgresql+psycopg://...
DATABASE_ECHO=False
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10

# AWS S3 설정
AWS_REGION=ap-northeast-2
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_S3_BUCKET_NAME=your_bucket_name
AWS_S3_UPLOAD_PREFIX=uploads
AWS_S3_PRESIGN_TTL_SECONDS=900
ALLOWED_UPLOAD_CONTENT_TYPES=["image/png", "image/jpeg"]

# Celery 설정
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

# GCP 설정
GCP_PROJECT_ID=your_project_id
```

### 3. 데이터베이스 설정

#### 데이터베이스 마이그레이션

```bash
# 마이그레이션 생성
alembic revision --autogenerate -m "migration message"

# 마이그레이션 적용
alembic upgrade head

# 마이그레이션 되돌리기
alembic downgrade -1
```

### 4. 서버 실행

#### FastAPI 서버

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 개발
fastapi dev app/main.py

# 공식
fastapi run
```

- 기본 라우터는 `/api/v1` 프리픽스가 적용됩니다.
- API 문서: `http://localhost:8000/docs`
- 헬스 체크: `GET http://localhost:8000/api/v1/healthz`
- DB 헬스 체크: `GET http://localhost:8000/api/v1/health/db`

#### Celery 워커

```bash
# Redis
redis-server

# Celery
celery -A app.worker.celery_app worker --loglevel=info
```

## API 엔드포인트

### 게임 관련 (`/api/v1/games`)

- `POST /api/v1/games` - 게임 생성
- `GET /api/v1/games/{game_id}` - 게임 상세 정보 조회
- `POST /api/v1/games/{game_id}/uploads/complete` - 업로드 완료 처리
- `GET /api/v1/games/{game_id}/uploads` - 업로드 상태 조회
- `POST /api/v1/games/{game_id}/stages/{stage_number}/check` - 정답 확인
- `POST /api/v1/games/{game_id}/stages/{stage_number}/complete` - 스테이지 완료
- `POST /api/v1/games/{game_id}/finish` - 게임 종료

### 헬스 체크

- `GET /api/v1/healthz` - 서비스 헬스 체크
- `GET /api/v1/health/db` - 데이터베이스 연결 상태 확인

## 개발 가이드

### 코드 스타일

- **Ruff**를 사용하여 코드 포맷팅 및 린팅을 수행합니다.
- 코드 포맷팅: `ruff format .`
- 린팅: `ruff check .`

### 타입 힌트

- Python 3.10+ 스타일 타입 힌트를 사용합니다 (`|` union, `list[int]` 등).
- 모든 공개 함수와 클래스에는 docstring을 작성합니다.

## TODO

- [ ] 인증/인가 추가
- [ ] 에러 핸들링 개선
- [ ] 로깅 시스템 구축
- [ ] 단위 테스트 및 통합 테스트 작성
