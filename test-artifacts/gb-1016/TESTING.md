# 电表累计读数核算 — 测试记录

测试对象：`backend/src/services/meter_engine.py`、`backend/src/services/meters.py`、
`backend/src/routes/meters.py`、`backend/src/worker.py` 与 `frontend/src/pages/meters.js`。

本地环境无 Docker，使用系统 Python 3.11 + pip 安装 fastapi 0.115.12、SQLAlchemy 2.0.41、
numpy、scikit-learn、httpx、pytest，数据库用 SQLite（生产为 MySQL 8.0）。
SQLite 不支持 `SELECT ... FOR UPDATE`，因此并发正确性同时由 `MeterState.lock_version`
乐观锁条件更新保证，并在真实线程测试中验证。

## 执行命令

```bash
python3 -m pytest test-artifacts/gb-1016/tests/ -q
python3 test-artifacts/gb-1016/tests/smoke_e2e.py
```

## 最近一次结果（2026-11-15，本机执行）

- `pytest`：**25 passed**
  - `test_meter_engine.py` 11 项：日耗=读数差、跨日缺测不均摊、守恒对账、回零/换表隔离、
    交接读数、迟到补录重建、同日多段累加、多电表、非法行校验、首读数日无日耗。
  - `test_meter_service.py` 9 项：草稿/发布物化、缺测无日耗、隔离无日耗、补录整组切换、
    令牌与发布幂等、重建不重复、合并仅取已发布完整日、读数依据追查、乐观锁并发拦截与恢复重试。
  - `test_meter_api.py` 5 项：viewer 只读权限、端到端发布与依据、令牌幂等、
    真实双线程并发发布无重复日耗、合并接口与原数据集导入并存。
- `smoke_e2e.py`：**SMOKE OK**。登录→接入（含 gap+isolated）→发布→读数依据→
  迟到补录重发布→启动恢复体检→合并生产特征→守恒核对全链路通过。
- 归因联动：用 159 天电表推导日耗跑通 `business.analyze`，留出期 R²=0.965、RMSE=15.81kWh。
- 前端：`node --check` 对 meters.js、main.js、entities.js、shell.js 语法检查通过。

## 未在本机执行的检查

- 真实 MySQL 行锁（`with_for_update`）与 Redis 队列、五容器 `docker compose` 健康检查、
  浏览器页面截图未在本机执行（环境无 Docker）；相关行为由 SQLite + 乐观锁测试与代码审查覆盖，
  容器级验收需在具备 Docker 的环境补做并回填本文件。
