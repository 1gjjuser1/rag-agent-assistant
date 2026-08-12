---
kind: dependency_management
name: Python 依赖管理：requirements.txt + pyproject.toml 双清单模式
category: dependency_management
scope:
    - '**'
source_files:
    - requirements.txt
    - requirements-dev.txt
    - pyproject.toml
---

## 1. 使用的系统与工具

本项目采用 Python 生态中经典的 **requirements.txt 清单 + pyproject.toml 配置** 组合来管理依赖，未使用 pipenv、poetry、uv 等更现代的依赖管理器。
- **运行时依赖**声明在 `requirements.txt`，通过 `pip install -r requirements.txt` 安装。
- **开发/测试依赖**声明在 `requirements-dev.txt`，通过 `-r requirements.txt` 引入运行时依赖后追加 pytest、ruff、mypy 等工具。
- **pyproject.toml** 仅用于配置 pytest、ruff、mypy 等工具的规则与目标文件范围，**不声明任何依赖包**（无 `[project.dependencies]` 段）。

项目目标 Python 版本锁定为 **3.10**（见 ruff `target-version = "py310"` 与 mypy `python_version = "3.10"`）。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `requirements.txt` | 运行时依赖清单，包含 LangChain、ChromaDB、DashScope、OpenAI、Streamlit、FastAPI、pymupdf、rapidocr 等 RAG/Agent 核心库 |
| `requirements-dev.txt` | 开发依赖，继承 `requirements.txt` 并追加 pytest、ruff、mypy |
| `pyproject.toml` | 统一配置 pytest、ruff、mypy 的行为与检查范围 |
| `.gitignore` | 忽略 `__pycache__`、`.mypy_cache`、`.ruff_cache` 等缓存目录 |
| `.env.example` | 提供环境变量模板（如 LLM API Key），配合 `python-dotenv` 加载 |

## 3. 架构与约定

### 版本约束策略：主版本号上限锁定
所有第三方包均使用 `>=X.Y.Z,<Z+1.0.0` 形式的**主版本区间约束**，例如：
- `langchain==1.3.14`（精确到次版本，避免大版本升级破坏）
- `chromadb>=1.0.0,<2.0.0`
- `fastapi>=0.115.0,<1.0.0`
- `streamlit>=1.40.0,<2.0.0`
- `openai>=2.0.0,<3.0.0`
- `dashscope>=1.24.0,<2.0.0`

这种写法允许小版本和补丁版本自动升级，但**禁止跨主版本升级**，防止上游 API 变更导致代码不可用。LangChain 单独使用 `==` 精确锁定，说明其作为核心框架对稳定性要求最高。

### 依赖分层
- **运行期**：`requirements.txt` 中的包是应用启动所需的完整集合。
- **开发期**：`requirements-dev.txt` 通过 `-r requirements.txt` 复用运行时依赖，再叠加测试与静态检查工具，形成清晰的 dev/runtime 分离。

### 无 vendoring / 无私有仓库
- 未发现 `vendor/`、`lib/` 等内嵌第三方源码目录。
- 未发现 `setup.py`、`setup.cfg`、`Pipfile`、`poetry.lock`、`go.mod` 等其他依赖声明文件。
- 未发现 `pip.conf`、`~/.config/pip/pip.conf`、`PYPI_URL` 等自定义源配置；依赖默认从 PyPI 拉取。
- 未发现 `requirements.txt` 中使用 `-e`（editable install）或 `-f/--find-links` 指向本地/私有仓库的用法。

### 环境隔离方式
项目未自带虚拟环境脚本（无 `venv/` 或 `.venv/`），开发者需自行创建虚拟环境后执行 `pip install -r requirements.txt`。`.env.example` 提示通过 `python-dotenv` 加载 `.env` 文件注入 LLM 密钥等敏感配置。

## 4. 约定与约束

- **Python 版本约束**：ruff 与 mypy 均锁定 `py310`，新增依赖必须兼容 Python 3.10。
- **主版本锁定**：除 langchain 使用精确版本外，其余依赖一律限制主版本上限，禁止跨主版本升级。
- **静态检查范围**：mypy 显式列出要检查的文件列表（`api.py`、`rag_pipeline.py`、`react_agent.py`、`store.py`、`vector_store.py`、`evals`、`utils` 等），`tests/` 目录被排除在外。
- **CI 集成**：`.github/workflows/ci.yml` 会执行依赖安装与测试流程（具体步骤以该 CI 文件为准），确保依赖可正常解析。
- **缓存清理**：`.gitignore` 忽略 `.mypy_cache`、`.ruff_cache`、`__pycache__`，避免污染仓库。

## 5. 风险与建议

- 缺少锁文件（`requirements.txt` 未配套 `requirements-lock.txt` 或 `pip freeze > requirements.txt`），不同环境可能解析出不同子版本，存在“在我机器上能跑”的风险。
- 未使用现代依赖管理工具（如 uv、pip-tools 的 `pip-compile`），依赖冲突排查成本较高。
- 若未来需要私有包或内部镜像，需在 CI 或本地 pip 配置中添加索引源，当前仓库未内置相关配置。