# Use supported candidates, then prove the combination

Checked 29 October 2026. The table gives a dated stable candidate set, not a universal “best” stack or a production-tested combination. M00 owns promotion into actual project manifests and locks. The deterministic core currently has no third-party runtime dependency.

| Component | Candidate | Primary evidence |
| --- | --- | --- |
| Python target | 3.13.18 | [Official security release announcement](https://blog.python.org/2026/10/python-3152-31410-31318-31216-31118/) |
| Node target | 24.22.1 LTS | [Official release](https://nodejs.org/en/blog/release/v24.22.1) |
| LangGraph | 1.2.18 | [PyPI](https://pypi.org/project/langgraph/) |
| SQLite graph saver | langgraph-checkpoint-sqlite 3.1.2 | [PyPI](https://pypi.org/project/langgraph-checkpoint-sqlite/) |
| FastAPI | 0.162.0 | [Official release notes](https://fastapi.tiangolo.com/release-notes/#01620) |
| Pydantic | 2.14.1 | [PyPI](https://pypi.org/project/pydantic/) |
| HTTPX | 0.28.1 | [PyPI](https://pypi.org/project/httpx/) |
| Uvicorn | 0.54.0 | [Official release notes](https://uvicorn.dev/release-notes/#0540-september-24-2026) |
| pytest | 9.2.0 | [Official changelog](https://docs.pytest.org/en/stable/changelog.html#pytest-9-2-0-2026-10-27) |
| React / React DOM | 19.3.1 / 19.3.1 | [Official release](https://github.com/react/react/releases/tag/v19.3.1), [DOM peer manifest](https://raw.githubusercontent.com/facebook/react/v19.3.1/packages/react-dom/package.json) |
| Vite | 8.3.5 | [Official tagged manifest](https://raw.githubusercontent.com/vitejs/vite/v8.3.5/packages/vite/package.json) |
| TypeScript | 7.0.2 | [Official package registry](https://registry.npmjs.org/typescript/latest) |

The prior core tests ran on **Python 3.13.2**. The source machine has **Node 22.14.0**. Target Python 3.13.18 and Node 24.22.1 were not installed or tested here; M00 must check distribution/Windows availability, install the declared runtime and record its actual version before claiming support. Staying on the Python 3.13 line limits migration scope; selecting Node 24 LTS is an engineering choice, not a claim it is the newest feature line.

## What was actually checked

The Python direct set in `version-candidates/requirements.in` successfully resolved together using uv compile targeting Python 3.13, producing hashed `requirements.resolved.txt`. The frontend direct set in `version-candidates/package.json` successfully resolved with an npm package-lock-only operation. Those files prove dependency resolution. They do not prove package installation, imports, SQLite checkpoint behavior, a server start, a TypeScript check, a Vite build or authenticated Meta behavior. Candidates were not applied to the root pyproject, and the frontend example is not a full application scaffold.

LangGraph may bring `langchain-core`, checkpoint packages and `langsmith` transitively. Accept compatible transitive dependencies through the resolver; do not install a second agent harness to satisfy their names. Do not enable vendor trace export merely because its client is present. React and DOM stay aligned. Vite's Node requirements and TypeScript tooling compatibility must pass actual install/build tests. ([Vite guide](https://vite.dev/guide/))

## M00 publishes phase A before integrated phase B

Phase A records platform and exact runtimes, installs the backend candidates together, runs library imports/strict schemas and the unchanged fourteen core tests, and publishes frozen interfaces plus phase-A locks. Resolve frontend plugins, React types, test and lint tools as M08 requires; exact pins remain unverified until resolved and installed. Phase A does not require downstream workflow, API or UI tests, so their implementation can begin.

Phase B is joint M00/M09 certification after M05 checkpoint/approval flow, M07 API and M08 UI exist: run checkpoint/recovery/approval, API contracts, frontend type checking/build and browser/API end-to-end cases using the integrated locks. Keep resolution-only, installed-tested, integrated-tested and live-tested statuses separate. No Meta key, model access or live tool/structured-output call was tested.

## Optional packages require a trigger

OpenTelemetry SDK **1.45.1** is a candidate when trace export is implemented; resolve its API/semantic-convention packages together. SQLAlchemy **2.1.6** is a candidate when a relational repository needs it; psycopg **3.3.6** follows the Postgres deployment milestone. These optional packages were not in the validated minimal solver set. Alembic has no asserted pin. ([OpenTelemetry](https://pypi.org/project/opentelemetry-sdk/), [SQLAlchemy](https://www.sqlalchemy.org/download.html), [psycopg](https://pypi.org/project/psycopg/), [Alembic](https://alembic.sqlalchemy.org/en/latest/changelog.html))

Vector search needs evidence structured retrieval is insufficient and an embedding/privacy gate. Redis needs demonstrated cross-worker coordination/cache demand. MCP needs a real external tool; A2A needs an independently hosted agent. NeMo needs selected-Meta configuration plus bypass/false-block tests. OpenShell needs qualified host support and process/network/filesystem enforcement tests. No exact NeMo/OpenShell pin or live Meta compatibility is asserted here; keep arbitrary-code upload disabled until execution-boundary tests pass.
