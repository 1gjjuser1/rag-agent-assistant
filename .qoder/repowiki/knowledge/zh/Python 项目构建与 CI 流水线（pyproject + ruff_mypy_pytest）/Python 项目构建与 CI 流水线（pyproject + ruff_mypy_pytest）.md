---
kind: build_system
name: Python 项目构建与 CI 流水线（pyproject + ruff/mypy/pytest）
category: build_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - requirements.txt
    - requirements-dev.txt
    - .github/workflows/ci.yml
    - .env.example
---

## 1. 使用的构建系统

该项目是一个纯 Python 应用，没有 Makefile、Dockerfile 或 shell 脚本。构建与质量保障完全基于以下工具链：
- **依赖管理**：`requirements.txt`（运行时）+ `requirements-dev.txt`（开发时，包含 pytest、ruff、mypy），通过 `pip install -r requirements-dev.txt` 一次性安装。
- **包配置**：`pyproject.toml` 集中声明了测试路径、Ruff 规则集、Mypy 检查范围与目标 Python 版本（3.10）。
- **CI 流水线**：`.github/workflows/ci.yml`，在 GitHub Actions 的 `ubuntu-latest` 上以 Python 3.10 运行，步骤为 checkout → setup-python (启用 pip cache) → 安装依赖 → ruff check → mypy → pytest → 离线评测 (`python evals/run_eval.py --offline`)。
- **无发布产物**：仓库未包含打包脚本、版本号管理文件或发布流程；该仓库定位为原型/示例工程，不执行 PyPI 发布或容器化构建。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | 定义 pytest 测试目录、Ruff 规则（E/F/I/UP/B/SIM，行宽 100，忽略 E501）、Mypy 检查文件列表与类型检查选项 |
| `requirements.txt` | 锁定运行时依赖（langchain、chromadb、fastapi、streamlit、dashscope/openai 等），使用 `>=x,<y` 的半开区间约束 |
| `requirements-dev.txt` | 引入 `requirements.txt` 并追加 pytest、ruff、mypy |
| `.github/workflows/ci.yml` | GitHub Actions 流水线，触发条件为 push main 与所有 PR |
| `.env.example` | 环境变量模板（LLM API Key 等），用于本地运行时的配置注入 |
| `evals/run_eval.py` | 评测入口，被 CI 以 `--offline` 模式调用作为 smoke test |

## 3. 架构与约定

- **单文件脚本式应用**：核心逻辑分散在根级 Python 文件（`app.py`、`api.py`、`rag_pipeline.py`、`react_agent.py`、`ingestion.py`、`store.py`、`vector_store.py`、`llm_client.py`、`config.py`），没有 `setup.py` / `pyproject.build-system` 的打包元数据，因此不存在可安装的 wheel/sdist。
- **测试组织**：`tests/` 目录下按模块划分测试文件（`test_agent.py`、`test_context.py`、`test_ingest.py`、`test_rag_answer.py`、`test_retrieval.py`、`test_smoke.py`、`test_store.py`），由 pytest 自动发现。
- **静态检查与类型检查分离**：Ruff 负责 lint（flake8 + isort + pyupgrade + bugbear + SIM），Mypy 仅对根级核心模块与 `evals`、`utils` 子包进行类型检查，显式排除 `tests/`。
- **CI 即唯一流水线**：所有质量门禁（lint、type check、unit tests、offline eval）都集中在 GitHub Actions 中，本地开发者需自行安装 `requirements-dev.txt` 后手动运行对应命令。
- **依赖版本策略**：全部使用 `>=X,<Y` 的兼容区间，避免锁死到具体小版本，便于安全更新但保留向后兼容性。

## 4. 约定与约束

- **Python 版本**：代码与 CI 均锁定为 Python 3.10（`target-version = "py310"`、`python_version = "3.10"`、`actions/setup-python@v5` with `python-version: "3.10"`）。
- **Lint 规则**：必须通过 `ruff check .`，启用 E/F/I/UP/B/SIM 规则集，行宽限制 100 字符（E501 被忽略）。
- **类型检查**：必须通过 `mypy .`，开启 `no_implicit_optional`，忽略缺失导入，排除 `tests/`。
- **测试运行**：通过 `pytest` 执行，测试目录固定为 `tests/`，默认静默输出并禁用警告。
- **CI 门禁**：push main 与所有 PR 都会触发完整流水线，任何一步失败将阻断合并。
- **环境变量**：运行时通过 `python-dotenv` 加载 `.env`，`.env.example` 提供键名模板；敏感信息不得提交至仓库。
- **无容器化/发布**：仓库不包含 Dockerfile、Makefile、release 脚本或版本标记流程，部署方式未在仓库内定义（推测由外部平台托管 FastAPI/Streamlit 进程）。