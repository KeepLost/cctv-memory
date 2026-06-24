# DEVELOPMENT.md - 开发环境、启动与工作流

## 0. 前提

- Python 3.12+
- uv（虚拟环境与依赖管理）
- ffmpeg / ffprobe（视频处理）
- SQLite 3.35+（FTS5、WAL、UPSERT 支持）

---

## 1. 项目初始化

使用项目本地 `.venv` 与 editable 安装（改完源码即时生效，无需重装）：

```bash
# 克隆后首次设置
cd codes/cctv-memory
uv venv

# 仅运行项目本体：
uv pip install -e .

# 开发（测试 / lint / 类型检查）：
uv pip install -e ".[dev]"
```

> `decode_backend=opencv` 是默认解码后端，`opencv-python-headless` 与 `numpy` 已是项目级必需依赖。
> `opencv-python-headless` 避免 GUI/X11 依赖，适合服务器/CI。

> 区分“安装方式”与“执行方式”：上面的 `uv pip install -e .` 是把项目以 editable 方式装进
> `.venv`；之后用 `uv run cctv-memory ...` 或 `.venv/bin/cctv-memory ...` 执行。`uv run` 是
> 执行方式，不是安装方式。面向用户的完整用法见 `docs/USAGE.md`。

pyproject.toml 应定义：

```toml
[project]
name = "cctv-memory"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "sqlalchemy>=2.0",
    "alembic>=1.13",
    "aiosqlite>=0.20",
    "httpx>=0.28",
    "opencv-python-headless>=4.9",
    "numpy>=1.26",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.5",
    "mypy>=1.10",
    "types-PyYAML>=6.0",
    "coverage>=7.5",
]
vec = [
    "sqlite-vec>=0.1",
]

[project.scripts]
cctv-memory = "cctv_memory.cli:main"
```

---

## 2. 日常开发

```bash
# 激活环境（uv 自动管理，无需手动 activate）
uv run pytest                      # 运行测试
uv run pytest -x -q                # 快速失败
uv run ruff check .                # lint
uv run ruff format .               # 格式化
uv run mypy cctv_memory/contracts cctv_memory/domain cctv_memory/application
```

---

## 3. 数据库 Migration

```bash
uv run alembic upgrade head        # 应用所有迁移
uv run alembic revision --autogenerate -m "add_xxx"  # 生成新迁移
```

规则：
- 每个 schema 变更必须对应 Alembic migration；
- migration 必须同时支持 SQLite 和未来 PostgreSQL 的可执行 DDL；
- 避免 SQLite 不支持的 ALTER TABLE 操作（如 DROP COLUMN）；对 SQLite 使用 batch mode。

---

## 4. 本地运行

```bash
# 初始化 data 目录
uv run cctv-memory init --data-dir ./data

# 启动服务（含内嵌 worker；host/port 默认 127.0.0.1:8080，来自 server 配置）
uv run cctv-memory serve --data-dir ./data --host 127.0.0.1 --port 8080

# 或独立 worker
uv run cctv-memory worker --data-dir ./data
```

`serve` 由 `cctv_memory/cli/__init__.py:_cmd_serve` 实现（FastAPI + uvicorn）。
完整、实测的启动/配置/真实 VLM/检索用法见 `docs/USAGE.md`。

---

## 5. 测试分层与运行

```bash
uv run pytest tests/unit/              # 单元测试
uv run pytest tests/schema/            # schema validation 测试
uv run pytest tests/contract/          # repository contract 测试
uv run pytest tests/security/          # 权限/安全测试
uv run pytest tests/search/            # search golden 测试
uv run pytest tests/integration/       # 集成测试
uv run pytest tests/architecture/      # 依赖方向测试
```

PostgreSQL / pgvector live tests are gated and must not print DSN values. They run only when a disposable PostgreSQL database with pgvector is available through `CCTV_MEMORY_TEST_POSTGRES_DSN`:

```bash
export CCTV_MEMORY_TEST_POSTGRES_DSN='postgresql+psycopg://user:password@localhost:5432/cctv_memory_test'
uv run pytest tests/integration/test_postgres_live.py -q
```

Without `CCTV_MEMORY_TEST_POSTGRES_DSN`, PostgreSQL live tests skip by design. These tests verify native PostgreSQL physical types such as `TIMESTAMPTZ`, `JSONB`, and `vector(N)`; do not weaken PostgreSQL DDL to SQLite-style `TEXT` to make adapter writes pass.

测试文件布局：

```text
tests/
  conftest.py                  # 公共 fixture：temp SQLite DB、mock VLM、test principal
  unit/                        # 纯逻辑单元测试
  schema/                      # Pydantic schema validation
  contract/                    # repository port contract tests
  security/                    # authorization / permission boundary
  search/                      # search golden fixtures + deterministic tests
  integration/                 # 端到端 API + worker 流程
  architecture/                # import direction enforcement
  fixtures/                    # 共享测试数据
```

---

## 6. 代码质量 Gate

合并前必须通过：

```bash
uv run ruff check .
uv run mypy cctv_memory/contracts cctv_memory/domain cctv_memory/application
uv run pytest --tb=short
```

mypy 至少覆盖 contracts/domain/application；infrastructure 层可渐进开启。

---

## 7. 环境变量

配置来源优先级：CLI/init 参数 > 环境变量 > `config.yaml` > 内置默认值
（见 `cctv_memory/config/settings.py`、`docs/contracts/configuration-contract.md §1`）。环境变量前缀
`CCTV_MEMORY_`，嵌套字段用双下划线 `__`。

常用环境变量：

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `CCTV_MEMORY_CONFIG_FILE` | YAML 配置文件路径（否则用 `./config.yaml`） | - |
| `CCTV_MEMORY_DATA_DIR` | 数据目录 | `./data` |
| `CCTV_MEMORY_LOG_LEVEL` | 日志级别 | `INFO` |
| `CCTV_MEMORY_ENV` | 环境标识 | `local` |
| `CCTV_MEMORY_VLM__PROVIDER` | VLM provider：`mock` 或 `real` | `mock` |
| `LLM_KEY` | 真实 VLM 的 API key（变量名由 `vlm.api_key_env` 决定，默认 `LLM_KEY`） | - |
| `CCTV_MEMORY_VLM_BASE_URL` | 真实 VLM 端点 URL（可选，变量名由 `vlm.base_url_env` 决定） | 见配置默认 |
| `CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE` | `ffprobe`/`static`/`ffmpeg_frames` | `ffprobe` |

注意：启用真实 VLM 需 `CCTV_MEMORY_VLM__PROVIDER=real` 且设置 `LLM_KEY`（或所配 `api_key_env`
指向的环境变量）。仅设置 key 而不切 provider 仍然走 mock。完整说明见 `docs/USAGE.md`。

Secrets 通过环境变量注入，不提交到代码仓库，也不写入 `config.yaml`。

---

## 8. 不使用 Docker

当前阶段不打包 Docker。部署方式：

- 本地开发：uv venv + `cctv-memory serve`
- 远程部署：systemd service 或 supervisor 管理进程
- 数据迁移：`cctv-memory backup` / `cctv-memory restore`

---

## 9. IDE 建议

- ruff 作为 formatter 和 linter（替代 black + isort + flake8）
- mypy 严格模式至少覆盖 contracts/domain/application
- pytest 自动发现 tests/
- pyproject.toml 统一配置 ruff/mypy/pytest
