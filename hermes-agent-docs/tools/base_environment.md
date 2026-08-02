# base.py - BaseEnvironment 学习笔记

## 一句话总结

`BaseEnvironment` 是 Hermes 的**底层执行引擎**，负责"在沙箱里安全执行 shell 命令"。它是基础设施层代码，不是 Agent 框架的一部分。做 Agent 开发**通常不需要深读**。

## 整体定位

```
┌──────────────────────────────────────┐
│  Agent 开发关心的层                    │
│  prompt / tools / skills / context    │  ← 你该学这个
│  approval gates / sub-agent / MCP     │
├──────────────────────────────────────┤
│  基础设施层（BaseEnvironment）          │
│  进程管理 / 管道通信 / CWD跟踪         │  ← 这是这层
│  中断信号 / 超时控制 / 沙箱隔离        │
└──────────────────────────────────────┘
```

**什么时候需要回来看这层：**
- 自己写 Agent 框架的执行引擎
- 给 Hermes 贡献代码修 shell 相关 bug
- 做安全审计（沙箱逃逸、进程隔离）
- 需要支持特殊环境（Windows Git Bash、Termux 等）

## 核心设计：spawn-per-call 模型

每条命令启动一个**全新的** `bash -c` 进程。这带来一个问题：shell 进程天生"失忆"——上一个进程里 `export TOKEN=abc`，下一个全新进程拿不到。

解决办法：**会话快照（snapshot）**

```
命令1: export TOKEN=abc
       → 执行完自动 export -p > 快照文件  # 保存状态

命令2: echo $TOKEN
       → 执行前 source 快照文件           # 恢复状态
       → 执行后 export -p > 快照文件      # 更新状态
```

## 一次 execute() 的完整流程

```
用户调用 execute("cd /project && python build.py")
              │
              ▼
  _wrap_command() 包裹成 bash 脚本：
  ┌──────────────────────────────────────┐
  │ source 快照文件          ← 恢复环境变量 │
  │ cd /上次的目录            ← 回到工位   │
  │ eval '用户命令'           ← 真正执行   │
  │ 记录退出码 $?                        │
  │ export -p > 快照文件      ← 保存新状态 │
  │ pwd -P > cwd文件 + stdout标记 ← 记录位置│
  │ exit $?                  ← 返回退出码 │
  └──────────────────────────────────────┘
              │
              ▼
  _run_bash() → 启动新 bash 进程
              │
              ▼
  _wait_for_process()
    - 每 5~200ms 轮询 "做完了吗"（自适应）
    - 用 select() 非阻塞读 stdout（防止后台进程挂起）
    - 超时？→ 杀进程，返回 124
    - 中断？→ 杀进程，返回 130
              │
              ▼
  _update_cwd() → 从输出中解析 __HERMES_CWD_xxx__ 标记，更新 self.cwd
```

## 类结构速览

| 方法 | 职责 |
|------|------|
| `__init__()` | 生成 session_id，设定快照/CWD 文件路径 |
| `init_session()` | 启动 login shell 拍快照（env/functions/aliases） |
| `_wrap_command()` | 把用户命令包裹成完整 bash 脚本（source+cd+eval+save） |
| `_wait_for_process()` | 轮询等待进程结束，处理超时/中断/stdout排空 |
| `_extract_cwd_from_output()` | 从 stdout 标记解析 CWD 并清理输出 |
| `execute()` | **统一入口**，编排以上所有步骤 |
| `cleanup()` | 抽象方法，子类实现资源释放 |

## 一些有价值的设计细节

### 1. 原子写入快照

两个命令并发执行时，不能用同一个临时文件名，否则互相覆盖。Hermes 用 `$BASHPID`（子进程真实 PID）而不是 `$$`（可能被继承）作为临时文件名后缀，保证并发安全。

```bash
# 正确：$BASHPID 每进程唯一
export -p > /tmp/snap.tmp.$BASHPID
mv -f /tmp/snap.tmp.$BASHPID /tmp/snap.sh  # mv 同盘原子操作
```

### 2. 非阻塞排空 stdout

如果用户的命令后台启动了子进程（如 `uvicorn & disown`），那个后台进程继承了 stdout 管道的写端。即使用户命令结束了，管道不 EOF，传统 `for line in proc.stdout` 会永久阻塞。

Hermes 用 `select()` 非阻塞轮询 + bash 退出后等 300ms 收集尾部 → 主动停止排空，不等待后台进程。

### 3. 孤儿进程防护

本地后端用 `os.setsid` 把子进程放进独立进程组。如果 Python 收到 SIGTERM 直接退出不 kill 子进程，子进程会被 init(PID=1) 接管成为孤儿。

`_wait_for_process` 中 `try/finally` + `KeyboardInterrupt/SysteExit` 捕获保证了 kill 后才退出。

### 4. 自适应轮询间隔

```
起始 5ms（快速命令如 echo 6ms 就返回）
 → 指数增长
 → 上限 200ms（长命令如 build 省 CPU）
```

## 子类一览

| 子类 | 文件 | 环境 |
|------|------|------|
| `LocalEnvironment` | `local.py` | 本机直接执行 |
| `DockerEnvironment` | `docker.py` | Docker 容器 |
| `SingularityEnvironment` | `singularity.py` | HPC 容器 |
| `SSHEnvironment` | `ssh.py` | 远程 SSH |
| `ModalEnvironment` | `modal_env.py` | Modal 云函数 |
| `DaytonaEnvironment` | `daytona_env.py` | Daytona 云环境 |

所有子类只需实现 `_run_bash()` 和 `cleanup()`，其余逻辑（快照、包裹、排空、CWD 提取）全部由基类提供。

## Agent 开发的学习建议

**这个文件不需要精读。** 优先级更高的内容：

1. prompt 拼装机制 — Agent 的"大脑"
2. tool/skill 定义注册 — Agent 的"手脚"
3. 上下文窗口管理 — Agent 的"记忆"
4. 审批门 — Agent 的"安全边界"
5. 子 Agent 调用 — Agent 的"协作能力"
6. MCP 协议 — 接入外部工具
