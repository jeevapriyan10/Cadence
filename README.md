# Cadence

> *It doesn't predict delays. It decides when the track is free.*

**Status: under active development**

Cadence is a railway block-window scheduling engine designed to compute conflict-free, optimal track occupancies and maintenance windows across complex rail networks.

---

## Architecture

*(Placeholder — architectural diagrams, solver models, and system design specifications will be documented here as development proceeds.)*

---

## Repository Structure

```text
cadence/
├── .github/
│   └── workflows/
│       └── ci.yml             # GitHub Actions CI workflow
├── backend/                   # Python 3.11 FastAPI backend
│   ├── cadence/               # Core engine package
│   ├── tests/                 # Backend unit & integration test suite
│   ├── .env.example           # Example environment variables
│   ├── pyproject.toml         # Package metadata and build configuration
│   └── requirements.txt       # Dependencies (fastapi, ortools, networkx, etc.)
├── frontend/                  # React + TypeScript + Vite web interface
│   ├── src/                   # Application source
│   ├── package.json           # Frontend dependencies and scripts
│   └── vite.config.ts         # Vite configuration
├── .gitignore
├── .env.example               # Root Docker Compose environment template
├── docker-compose.yml         # Multi-container orchestration (Postgres, API, SPA)
├── LICENSE                    # MIT License
└── README.md
```

---

## Running with Docker

Cadence provides a one-command, containerized deployment orchestration via Docker Compose that spins up PostgreSQL 16, the FastAPI backend, and the React frontend served by Nginx.

### Quick Start

```bash
# Optional: customize credentials if desired
cp .env.example .env

# Build images and start the full stack
docker-compose up --build
```
*(Alternatively, `docker compose up --build` on modern Docker CLI installations).*

### Service Endpoints & Ports

| Service | Host URL | Description |
|---|---|---|
| **Frontend** | [http://localhost:3000](http://localhost:3000) | React SPA served by Nginx with reverse proxy to `/api/*` |
| **Backend API** | [http://localhost:8000](http://localhost:8000) | FastAPI solver engine & REST API ([Swagger docs](http://localhost:8000/docs)) |
| **PostgreSQL** | `localhost:5432` | PostgreSQL 16 database (`cadence`, credentials in `.env.example`) |

### How Docker Differs from Local Development

| Aspect | Containerized Stack (`docker-compose`) | Local Development Workflow |
|---|---|---|
| **Database** | **PostgreSQL 16** container with persistent volume (`postgres_data`). Database migrations (`alembic upgrade head`) execute automatically on container startup before API server launches. | **SQLite** (`sqlite:///./cadence.db`) by default. Requires zero background services and works instantly for offline unit tests. |
| **Networking & Reverse Proxy** | Single frontend origin on port `3000`. Nginx serves the production-bundled SPA static assets and reverse proxies `/api/*` requests internally to `http://backend:8000`, eliminating browser CORS handling. | Two separate dev servers: Vite on `http://localhost:5173` and Uvicorn on `http://localhost:8000`, communicating across ports via CORS headers. |
| **Configuration** | Driven by environment variables (`DATABASE_URL`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `VITE_API_BASE_URL`). | Defaults to file-based SQLite and localhost URLs without requiring any environment setup. |

To stop the containers:
```bash
docker-compose down
```
To stop the containers and wipe persistent PostgreSQL database volumes:
```bash
docker-compose down -v
```

---

## Local Development (Without Docker)

### Prerequisites
- Python 3.11+
- Node.js 20+ and npm

### Backend Setup
```bash
cd backend
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Unix/macOS:
source .venv/bin/activate

pip install -r requirements.txt
pytest
```

### Frontend Setup
```bash
cd frontend
npm install
npm test
npm run dev
```

---

## License

[MIT](LICENSE) © 2026 jeevapriyan10