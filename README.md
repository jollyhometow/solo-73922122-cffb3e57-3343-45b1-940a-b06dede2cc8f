# SQLite 复现库缩减服务

入仓开发者排查 SQL 失败时，用它把精简后的**复现库**交给同事：上传一个结构受限、
外键有效的 SQLite 数据库和一段确定性 SQL 脚本，服务先确认原始错误，再在保持
表结构与剩余行值不变、外键始终有效的前提下删除数据行，直到得到 **1-最小**
复现库——删除任意一行（连同直接或间接引用它的全部行）后都无法再复现同一错误。

## 复现判定（严格）

* 以 SQLite **扩展错误码 + 完整错误文本** 作为错误指纹，两者完全一致才算复现；
* 错误码不同、错误文本不同、脚本执行成功、试跑超时，都不算复现；
* 原脚本执行成功时直接返回“无需缩减”，不产出下载库。

## 输入约束

| 项目 | 限制 |
| --- | --- |
| 普通表数量 | ≤ 3（禁止视图、虚拟表、触发器、附加库） |
| 全部表总行数 | ≤ 60 |
| 主键 | 每张表有且仅有一个单列 `INTEGER PRIMARY KEY`（禁止 WITHOUT ROWID） |
| 外键 | 全部为单列 `INTEGER`，且引用目标表的单列整数主键；**允许循环引用** |
| 外键动作 | 仅允许 NO ACTION / RESTRICT（禁止 CASCADE/SET NULL 等） |
| 现有数据 | 上传库必须已通过 `PRAGMA foreign_key_check` |
| SQL 脚本 | UTF-8 文本、确定性、不访问外部文件 |

脚本安全由两层保证：

1. 静态检查拒绝 `ATTACH/DETACH`、除 `foreign_keys` 外的 `PRAGMA`、
   `load_extension/readfile/writefile`、`VACUUM INTO` 等；
2. 每个试跑连接安装 SQLite authorizer 作为运行期最终防线，
   并强制 `PRAGMA foreign_keys=ON`。

## 隔离保证（先 COMMIT 后失败也安全）

* 上传原件落盘后设为只读；
* 先复制出一份**规范原件**（`journal_mode=DELETE`、完整性检查通过）；
* **每一次试跑都从规范原件复制全新临时文件**，脚本在副本里执行，
  结束后删除副本及 `-wal/-shm/-journal` 侧车文件；
* 因此脚本即使先 `COMMIT` 再失败，已提交的修改也只落在临时副本上，
  不会污染原件、后续试跑或最终下载库；
* 下载库由规范原件按保留行集合重新生成，并再次外键校验、完整性检查和试跑确认。

## 缩减算法

行级 Delta Debugging，且每次删除都沿外键反向边做**闭包扩张**
（删除被引用行时连带删除所有直接/间接引用者），保证剩余数据外键有效：

1. **ddmin 阶段**：对当前保留行集合二分切片，尝试删除一个分块或其补集；
   复现同一错误就采纳并回到粗粒度，否则细化粒度直到单元素；
2. **逐行终检**：对每个保留行尝试删除其外键闭包，反复整轮扫描，
   直到一整轮没有任何可删集合——即满足 1-最小性；
3. 产出最终库后再跑一次脚本，确认错误码与完整错误文本与基线一致。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

页面支持：上传库与脚本（文件或粘贴文本）、运行、轮询查看删减结果与日志、
下载复现库 `repro.db`，并内置两个可一键试跑的样例。

停止与清理：

```bash
docker compose down          # 停止容器（保留数据卷）
docker compose down -v       # 同时删除任务数据卷
```

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest             # 48 个测试
```

## 内置样例

| 样例 | 说明 |
| --- | --- |
| `cyclic` | 三表循环外键（dept ↔ employee ↔ project），脚本触发主键唯一约束错误 |
| `commit_then_fail` | 自引用循环外键，脚本先 `COMMIT` 一个 DELETE，再触发 NOT NULL 错误，验证副本隔离 |

样例文件由 `app/samples.py` 在启动时生成于 `app/static/`，可单独下载。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAX_DB_BYTES` | 20971520 | 上传库大小上限（字节） |
| `MAX_SCRIPT_BYTES` | 262144 | 脚本大小上限（字节） |
| `MAX_TRIALS` | 800 | 单次任务试跑次数上限 |
| `TRIAL_TIMEOUT_SECONDS` | 15 | 单次试跑超时 |
| `JOB_TTL_SECONDS` | 3600 | 任务结果保留时长 |
| `MAX_CONCURRENT_JOBS` | 2 | 后台并发任务数 |
| `DATA_DIR` | `/data` | 任务工作目录（容器内挂载为数据卷） |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 单页前端 |
| GET | `/api/samples` | 内置样例清单与下载地址 |
| POST | `/api/jobs` | 表单字段 `db`（必填）、`sql`（文件）或 `script_text`（文本），返回 202 与任务 id |
| GET | `/api/jobs/{id}` | 查询任务状态、错误指纹、行数变化、保留行与缩减日志 |
| GET | `/api/jobs/{id}/download` | 下载复现库 `repro.db`（仅缩减成功时可用） |

## 目录结构

```
app/
  config.py        # 约束与运行配置
  errors.py        # 业务异常
  validation.py    # 库结构/行/外键图校验
  script_guard.py  # 脚本静态检查
  runner.py        # 规范副本、authorizer、独立副本试跑、确定性双跑
  reducer.py       # ddmin + 逐行闭包终检
  jobs.py          # 后台任务与并发/TTL 管理
  service.py       # 上传->校验->确认->缩减 流水线
  samples.py       # 内置样例生成
  main.py          # FastAPI 路由
  static/index.html
tests/             # 48 个单元/集成/端到端测试
Dockerfile
docker-compose.yml
```
