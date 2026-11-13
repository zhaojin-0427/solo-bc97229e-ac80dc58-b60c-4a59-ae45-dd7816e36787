# 退货单 SQLite 切片服务

售后系统测试数据提取工具：在 Web 页面输入退货单 ID，后台从**部署配置的 SQLite 源库**
中抽取该退货单的关联数据切片，产出一个结构与源库一致、通过外键检查的 SQLite 文件，
用于离线复现退款错误。

## 一、能力对照（需求逐条）

| 需求 | 实现方式 |
|---|---|
| Flask 切片网页，读取部署配置的 SQLite 源库 | `app/web.py`，源库路径由环境变量 `SLICE_SOURCE_DB` 配置 |
| 六表（退货单/退货明细/订单/订单行/客户/商品），整数 id 主键 | `app/schema.py` 启动时内省校验，结构不符直接拒绝任务 |
| 单列外键可引用 id **或其它单列唯一键**（退货业务号、客户业务码） | 内省读取 `PRAGMA foreign_key_list` 的父子列声明，并校验父列确为单列唯一键（主键或单列 UNIQUE 索引）；复合外键、非唯一父列直接拒绝 |
| 明细选取与递归闭包**遵守外键声明** | 种子明细按“退货明细→退货单”的外键声明关联（如 `return_order_id=700 → return_no='700'`）；扩展阶段逐条外键用 SQL 连接解析到父表 id |
| 按父列**类型亲和性、排序规则**匹配（数字文本、大小写） | 比较由 SQLite 完成且父列固定在等号左侧：`'700'` 与 `700` 跨亲和性命中，`'ALPHA'` 与 `'Alpha'` 按 NOCASE 命中；不改原值、不引入无关行 |
| 保留指定退货单及**全部明细**，再递归保留外键指向记录 | 种子 = 退货单本身 + 按外键声明选出的全部 `return_lines`，之后 BFS 沿外键正向扩展 |
| 恰好导出此集合，**不反向带入其他明细** | 只做正向外键遍历，无反向 JOIN；兄弟退货单、未退货订单行不会进入切片（有测试断言） |
| 保留六表字段、主外键与唯一键定义及原值 | 直接复用 `sqlite_master` 中的原始 CREATE 语句与索引，按原列顺序、原值 INSERT（存储类型与大小写都不改写） |
| 生成通过外键检查的 SQLite 文件 | 加载后执行 `PRAGMA foreign_key_check` + `integrity_check`，全过才原子改名 |
| 客户推荐关系可成环（含 NOCASE 业务码互引） | 候选目标解析为父表整数 id 后入 `seen` 临时表去重，BFS 自然收敛；加载期关外键以容纳循环外键，加载后统一校验 |
| 抽取期间源库可继续增删改，整份切片来自**同一快照** | 每任务一个 `mode=ro` 只读连接，显式 `BEGIN` 后首条读语句固定 WAL 读标记，全程不关闭 |
| 不能把全部选中 ID 或记录装入内存 | 遍历状态在 SQLite **磁盘临时表**（`temp_store=FILE`），Python 每批只取 `BATCH_SIZE` 行；拷贝按键集分页流式进行。实测 5 千→8 万行闭包，Python 峰值 0.10→0.12 MB |
| 缺失引用或执行中断则整次失败，不提供残件 | 先写 `*.partial` 临时库，任何异常立即删除；只有全部检查通过才 `os.replace` 原子改名 |
| 重启后成功状态与完整文件保持一致 | 启动恢复：在途任务重置 failed、清理残件；success 但文件丢失则降级 failed；孤儿文件清理 |
| 网页查看进度、各表行数并下载 | 首页轮询 `/api/jobs`，展示阶段进度/六表行数，成功后一键下载 |
| 附结构、业务号外键、循环引用、持续修改功能的本地示例源库 | `app/demo.py` + `scripts/build_demo_db.py`：明细经业务号 `return_no='700'` 关联退货单 id=1，客户业务码 NOCASE 两环/自环，网页内置持续写入器 |
| 可实际启动的 Docker Compose 与可演示 Web 入口 | `Dockerfile` + `docker-compose.yml`，浏览器访问 `http://localhost:8000` |

## 二、目录结构

```
app/
  __init__.py     Flask 应用工厂与配置
  schema.py       源库结构内省（主键/外键/唯一键/索引校验）
  extractor.py    核心：快照 BFS 闭包遍历 + 流式拷贝 + 完整性闸门
  jobs.py         任务状态库、后台线程、崩溃恢复、原子产出
  demo.py         示例源库构造（业务号外键 + NOCASE 环）与持续写入模拟器
  web.py          路由与 JSON API
  templates/      单页界面
  static/         原生 JS/CSS（无构建步骤）
scripts/
  build_demo_db.py  示例库生成 CLI（--orphan 可造断引用）
  entrypoint.sh     容器入口（首启自动建示例库）
tests/            35 个 pytest 用例
Dockerfile, docker-compose.yml, requirements.txt, wsgi.py, run.py
```

## 三、Docker Compose 启动（推荐）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

- 首次启动自动在卷 `slice-data` 中创建示例源库 `/data/source.db`
- 想同时运行持续修改源库的演示容器：

```bash
docker compose --profile writer up --build
```

- 挂载已有源库：修改 `docker-compose.yml` 的 volumes，把宿主库挂到
  `/data/source.db`（或改 `SLICE_SOURCE_DB` 指向挂载路径）

## 四、本地运行

```bash
pip install -r requirements.txt
python scripts/build_demo_db.py /tmp/demo/source.db         # 构建示例源库
SLICE_SOURCE_DB=/tmp/demo/source.db SLICE_DATA_DIR=/tmp/demo/data python run.py
# 生产 WSGI：
SLICE_SOURCE_DB=/tmp/demo/source.db SLICE_DATA_DIR=/tmp/demo/data \
  gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 4 wsgi:app
```

> 注意：gunicorn 必须保持 `--workers 1`（任务队列在进程内，多 worker 会各自持有
> 独立队列；并发能力由 `--threads` 提供，任务状态与切片都在磁盘，重启不丢）。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SLICE_SOURCE_DB` | `/data/source.db` | 源 SQLite 库路径（只读打开） |
| `SLICE_DATA_DIR` | `/data` | 任务库与切片输出目录（切片在 `$DIR/slices/`） |
| `SLICE_BATCH_SIZE` | `500` | 每批从临时表取出的行数（内存上限旋钮） |
| `SLICE_MAX_WORKERS` | `2` | 后台抽取线程数 |

## 五、HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 操作页面 |
| GET | `/api/source` | 源库六表当前行数 |
| POST | `/api/jobs` | 提交任务，body `{"return_order_id": 1}` |
| GET | `/api/jobs` | 任务列表（状态、进度、各表行数） |
| GET | `/api/jobs/<id>` | 单任务详情 |
| GET | `/api/jobs/<id>/download` | 下载切片（仅 success，否则 409/410） |
| POST | `/api/demo/start` `/stop` `/tick` | 持续写入器控制（演示快照隔离） |
| GET | `/api/demo/status` | 写入器状态 |

## 六、核心算法说明

### 1. 快照一致性
每个任务独占一个 `file:...?mode=ro` 只读连接，`BEGIN` 后立即执行一条读语句，
在 WAL 模式下该连接的读标记（read mark）就此固定；连接在整个遍历+拷贝期间保持
打开，源库后续的提交对它不可见。任务结束才关闭连接。

### 2. 外键声明驱动的闭包
内省阶段从 `PRAGMA foreign_key_list` 读出每条单列外键的**子列 → 父列**声明，
并校验父列是父表的单列唯一键（主键 `id`，或 `return_no`、`code` 这类带
UNIQUE 约束/唯一索引的业务键；`REFERENCES t` 未列名时按父表主键处理）。

- **种子明细**：退货单按整数主键 `id=?` 定位后，用
  `return_lines JOIN return_orders ON 父列 = 子列` 选出它的全部明细——
  明细引用业务号（如 `return_order_id=700 → return_no='700'`）时同样命中，
  不会再出现“成功导出零条明细”。
- **递归扩展**：对每批行逐条外键做两件事——先反连接检查
  （非空外键在父表快照中无目标即整次失败），再 `JOIN` 父表把目标解析为
  父表整数 `id` 入队。所有比较父列固定在等号左侧，因此采用父列的排序规则
  （如 NOCASE）；类型亲和性按 SQLite 规则对两侧对称生效，数字文本
  （`'700'` 与 `700`）自动归一匹配。合法引用不再被误报缺失。
- **不改原值**：匹配只用于定位父行 id，拷贝阶段按原列顺序原值 INSERT，
  文本大小写与存储类型都与源库一致。

### 3. 有界内存 BFS
四个磁盘临时表承担全部遍历状态（`PRAGMA temp_store=FILE`，均为
`WITHOUT ROWID` 复合主键表）：

- `seen(表,id)`：已纳入闭包的全部行，天然去重（成环收敛）
- `frontier(表,id)`：待处理队列
- `batch(表,id)`：本批最多 `BATCH_SIZE` 行
- `cand(表,id)`：本批外键指向的候选目标（已解析为父表整数 id）

每轮：取一批 → 逐外键校验并收集候选 → 去重入队。
Python 进程任何时刻只持有当前批次；数据拷贝用 `id > ? LIMIT ?` 键集分页。

### 4. 原子产出与失败语义
`$DATA/slices/slice-<jobid>.db.partial` 上构建（关外键加载以支持环），
完成后重新打开外键执行 `foreign_key_check` 与 `integrity_check`；
通过 → `os.replace` 原子改名；任何异常 → 删除残件、任务置 failed。

## 七、测试

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

35 个用例覆盖：

- `test_business_key_fk.py`（本次回归）：业务号种子选取（id=1/业务号 700 不再漏导）、
  数字文本双向亲和性匹配、NOCASE 业务码环不再误报缺失、业务键真缺失整次失败、
  非唯一父列/复合外键拒绝、`REFERENCES t` 缺省主键、唯一索引父键、约束原样保留
- `test_closure.py`：闭包精确性、客户两环/自环、不反向带入兄弟退货单与未退货明细、
  结构/原值保留
- `test_failure.py`：种子缺失、遍历中断引用、脏库、坏结构 → 整次失败且无残件
- `test_snapshot.py`：抽取期间写入器并发增删改，切片仍等于快照时刻；重复抽取一致
- `test_recovery.py`：成功任务重启一致、中断任务重启置 failed、success 文件丢失降级
- `test_web.py`：全部 HTTP API 与下载内容的外键检查
- `test_bounded_memory.py`：4 万客户长环 + 万级明细，Python 峰值内存不随闭包增长

### 手动演示失败路径

```bash
python scripts/build_demo_db.py --orphan /tmp/demo/orphan.db
# 把 SLICE_SOURCE_DB 指向它，提交 return_order_id=1：
# 明细 99 指向不存在的订单行 9999，任务整次失败且无残件
```
