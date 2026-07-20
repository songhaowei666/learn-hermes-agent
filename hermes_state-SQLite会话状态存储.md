# hermes_state.py — SQLite 会话状态存储引擎

> 文件位置：`hermes_state.py`（约 7178 行）
> 分析日期：2026-07-20

---

## 一、模块定位

`hermes_state.py` 是 Hermes Agent 的**持久化会话存储层**，用 SQLite + FTS5 全文搜索替代了原来每个会话一个 JSONL 文件的方案。CLI 和 Gateway（Telegram/Discord）的会话元数据、完整消息历史、模型配置全部存在一个 SQLite 文件里。

一句话概括：**Hermes 所有会话数据的唯一真相源，95 个方法基本都是「拼 SQL → 执行」的薄封装。**

---

## 二、文件结构

```
├── 模块级工具函数（1-936 行）
│   ├── workspace_key()          — 提取会话的 workspace 分组键
│   ├── apply_wal_with_fallback() — WAL 兼容性处理（NFS 降级到 DELETE 模式）
│   ├── repair_state_db_schema()  — 修复损坏的 DB schema（不丢数据）
│   ├── is_malformed_db_error()   — 判断是否是 schema 损坏错误
│   ├── get_last_init_error()     — 读最后一次初始化错误
│   └── format_session_db_unavailable() — 格式化 "DB 不可用" 错误消息
│
├── class SessionDB（937-7163 行）— 同步数据库操作类，约 90 个公开方法
│
└── class AsyncSessionDB（7164-7178 行）— 异步门面，15 行
    └── __getattr__ 拦截所有方法 → asyncio.to_thread() 丢进线程池
```

---

## 三、核心设计决策

| # | 决策 | 落地点 |
|---|------|--------|
| 1 | **WAL 模式** | 多读者 + 一写者并发，Gateway 多平台同时读写不互斥 |
| 2 | **FTS5 全文搜索** | 跨所有会话消息的倒排索引，`search_messages()` 入口 |
| 3 | **parent_session_id 链** | 压缩时创建子会话指回父会话，不可变日志 + 可追溯血缘 |
| 4 | **不存批处理和 RL 轨迹** | 职责边界声明，避免 schema 膨胀 |
| 5 | **source 标记** | `'cli'` / `'telegram'` / `'discord'` 等，支持按来源过滤 |
| 6 | **随机抖动重试** | SQLite 超时设 1s，写冲突在应用层随机抖动重试最多 15 次，避免 convoy effect |
| 7 | **NFS 降级** | WAL 在 NFS/SMB 上不可用时自动退到 DELETE journal mode |

---

## 四、公开方法分类（约 95 个）

### 写方法模板（占 60%）

```python
def some_write(self, session_id: str, ...) -> None:
    def _do(conn):
        conn.execute("UPDATE/INSERT/DELETE ... WHERE ...", (params,))
    self._execute_write(_do)
```

所有写操作统一走 `_execute_write()`，内部处理 `BEGIN IMMEDIATE → 执行 → COMMIT` + 随机抖动重试。

### 读方法模板（占 30%）

```python
def some_read(self, ...) -> Optional[Dict]:
    with self._lock:
        cursor = self._conn.execute("SELECT ... WHERE ...", (params,))
        row = cursor.fetchone()  # or fetchall()
    return dict(row) if row else None
```

### 方法按功能域分组

| 功能域 | 方法数 | 代表方法 |
|--------|--------|----------|
| 生命周期 | 7 | `create_session`, `end_session`, `reopen_session`, `delete_session` |
| 查询检索 | 8 | `get_session`, `resolve_session_id`, `get_session_by_title` |
| 列表浏览 | 6 | `list_sessions_rich`（最核心的列表方法）, `list_recent_user_messages` |
| 全文搜索 | 4 | `search_messages`（FTS5 + trigram + CJK LIKE 三路径） |
| 消息读写 | 10 | `append_message`, `replace_messages`, `get_messages`, `get_messages_as_conversation` |
| 压缩与状态 | 13 | `try_acquire_compression_lock`, `get_compression_lineage`, `archive_and_compact` |
| 元数据更新 | 10 | `update_session_meta`, `set_session_title`, `update_token_counts` |
| Gateway 专用 | 8 | `find_session_by_origin`, `save_gateway_routing_entry` |
| Telegram Topic | 11 | `bind_telegram_topic`, `is_telegram_topic_mode_enabled` |
| 导入导出 | 10 | `export_session`, `import_sessions`, `export_all` |
| 维护清理 | 5 | `vacuum`, `optimize_fts`, `delete_empty_sessions` |
| 跨平台 Handoff | 6 | `request_handoff`, `claim_handoff`, `complete_handoff` |
| KV 存储 | 2 | `get_meta`, `set_meta` |

---

## 五、真正需要重点阅读的 5 个方法

其余 90 个方法都是模板变体，用到时按名搜索看 SQL 即可。

| 优先级 | 方法 | 行号 | 预计耗时 | 为什么 |
|--------|------|------|----------|--------|
| 1 | `_init_schema` | 1420 | 20min | **所有表结构、索引、触发器的 DDL 全集**，读完就知道数据库长什么样 |
| 2 | `__init__` | 979 | 10min | 初始化全流程：连接 → WAL/降级 → schema → FTS5 → 列校准 |
| 3 | `_execute_write` | 1217 | 10min | 写路径的秘密：随机抖动重试、WAL checkpoint 节奏、FTS5 optimize 节奏 |
| 4 | `search_messages` | 4830 | 20min | 最复杂的读方法，FTS5 + trigram + CJK 降级 LIKE 三路径，唯一突破模板的方法 |
| 5 | `get_session` | 2899 | 3min | 定义了"一个会话长什么样"的返回结构，所有上层调用者依赖这个结构 |

**这 5 个方法合计约 400 行，1 小时左右读完，之后随便点开哪个方法看 SQL 就行。**

---

## 六、与本模块无关的职责

- **不存批处理结果**：batch runner 有独立存储
- **不存 RL 训练轨迹**：state-action-reward 序列走独立系统
- **不做安全判断**：只管存储，安全扫描在 `tools/tirith_security.py`
- **不做消息压缩算法**：压缩逻辑在 `agent/memory_manager.py`，这里只存压缩结果

---

## 七、相关文件

- [agent/memory_manager.py](../hermes-agent-main/agent/memory_manager.py) — 消息压缩与摘要逻辑
- [hermes_constants.py](../hermes-agent-main/hermes_constants.py) — `get_hermes_home()` 等常量
- [tools/tirith_security.py](../hermes-agent-main/tools/tirith_security.py) — 命令安全扫描（独立系统）
- [agent/curator.py](../hermes-agent-main/agent/curator.py) — 后台技能维护（也依赖 SessionDB）
