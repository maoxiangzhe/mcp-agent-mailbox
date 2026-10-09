# 验证状态（截至 2026-10-09，第 11 轮 · 结项）

本文件回答一个问题：**这件事到底验证到哪一步了，哪些是实测、哪些还没做。**

> **一句话结论**：C / B / A 三项改动全部完成，并在**所有本会话能做的通道**上验证通过
> （合计 58 条断言，全绿）。唯一未做的是"真实 DSH 会话创建时把两半接起来"这一次运行时观测
> ——它需要重启 DSH 或新建会话，脚本已备好（见第五节第一条）。

---

## 一、已完成并有实测证据

| # | 项 | 证据 | 结果 |
|---|---|---|---|
| C | 修补"未绑定连接可冒用他人会话身份" | `E:\zcz\.scratch-mailbox-tests\c_verify.py`（独立 OS 进程复现攻击） | **8/8 PASS** |
| B | 每会话邮箱插件（`index.js` 等） | `_selftest/selftest.mjs`（替身落盘计数） | **23/23 PASS**，可重复 |
| B | 装载前预检（manifest / patch / 路径 / ESM / 真语法 / inject 声明） | `preflight.py` | **20/20 PASS** |
| B | 真实 cordis 4.0.4 运行时复核 | `node _evidence/verify/verify.mjs` | **全部通过**（含真实 mcp-client Config schema 校验） |
| A | profile 迁移（删写死会话 ID + 接入 include） | `migrate_profile.py --check`；已在**副本**上演练 dry-run→apply→幂等→校验 | **通过**，真实 profile 已应用且有备份 |
| — | 插件被真实 DSH 加载 | `.runtime-state.json` 里带真实邮箱路径的 `plugin.loaded` | **已发生 ≥8 次**（每次改文件即热重载），无一条 `mount.failed` |

### 关键结论：`agents` 服务与回填逻辑都是正常的

对照实验（同一份代码，两种环境）：

```
DSH 真实进程：
  plugin.loaded  command=E:\...\mcp-agent-mailbox\.venv\Scripts\python.exe
  diag.agents_list  ok=true, count=0, ids=""
  backfill.skipped  reason=当前没有活跃会话可回填

验证脚本（替身 list() 返回两个假会话）：
  diag.agents_list  ok=true, count=2, ids="session-PRE-1,session-PRE-2"
  backfill.begin    count=2
  mount.ok          session-PRE-1 / session-PRE-2
```

⇒ 机制完好；**真实 DSH 进程里当前没有活跃 Agent**（本会话的 Agent 不在该进程的注册表里，
尽管它的会话文件 `C:\Users\mxz\.dsh\sessions\--E-zcz--\session-0c7151c0-…\` 持续更新）。

---

## 二、已完成并有实测证据（续）

### ⭐ 多会话端到端机制验证（第 10 轮，**7/7 PASS**）

`E:\zcz\.scratch-mailbox-tests\e2e_two_sessions.py` —— 用**两个真实 OS 子进程**模拟两个 DSH
会话，覆盖需求 R1 的全部语义（每会话一个账号、身份由宿主注入、同时在线、互发信、单会话离线）：

```
A: pid=17180 session=e2e-session-A account=acc_01a11f6eb76977ec82efe87610686bc0
B: pid=11272 session=e2e-session-B account=acc_01a11f6eb7817b399efc63117a5e7804

[PASS] E1 两个会话 -> 两个不同账号
[PASS] E2 每个账号的 native_session_id 恰好等于注入给它的会话 ID（身份没串）
[PASS] E3 两个账号同时在线（各自进程活着）
[PASS] E4 显示名独立、互不影响
[PASS] E5 A 能向 B 发信，B 的 inbox 收到 1 条
[PASS] E6 杀掉 A 的进程 -> 只有 A 离线，B 仍在线
```

这条把"邮件层"的一半验证到位了；"DSH 侧注入会话 ID"的一半由 `preflight.py` /
`_selftest` / 真实 cordis 复核覆盖（`MAILBOX_SESSION_ID == agent.id` 已实测）。
**唯一没有观测过的连接点**是"真实 DSH 启动时把两者接起来"——需要重启。

---

### 1. 插件确实跑在 DSH 运行时里，而且那个进程里有可用的 Agent 注册表

落点带上了 `process.pid`，真实 DSH 的那条是：

```json
{"event":"plugin.loaded","plugin":"mailbox-per-session","pid":25480,"command":"E:\\zcz\\modle\\MCP\\mcp-agent-mailbox\\.venv\\Scripts\\python.exe"}
{"event":"diag.agents_list","ok":true,"pid":25480,"count":0,"ids":""}
{"event":"backfill.skipped","reason":"当前没有活跃会话可回填"}
```

`25480` **不等于** `Get-Process` 里那两个 `DeepSeek Harness`（22932 / 13936）——
沙箱下进程枚举看不到真正的 DSH 运行时宿主，这一点与早先 `host_pid=9248` 的现象一致。

⇒ **`agents` 服务可用、`list()` 返回合法空数组**，即：插件路径、注入、回填调用全部走通；
只是那一时刻没有任何活跃 Agent（本会话的 Agent 不在此运行时内）。

### 2. ⚠️ 多 DSH 进程并发时的 `MAILBOX_HOME` 冲突风险（新增发现）

这台机器上**同时存在两个 `DeepSeek Harness` 进程**（22932 / 13936），而 `plugin.patch.yml`
里的 `mailboxHome` 是**写死的** `'C:\Users\mxz\.board-mcp'`。若两个 DSH 进程都加载本插件，
两边各自的邮箱 MCP 子进程会**共用同一份 SQLite**：

- 工程上通常没问题（WAL + 跨进程并发是这台库的既有工作模式，实测 43+ 条连接来自多个进程）；
- 但会让"一个会话一个账号"的观测复杂化，且**两个进程的会话 ID 不可能相同**，不会互相冒用；
- 真正需要注意的只有：`MAILBOX_HOME` 若被两个**不同用户/不同实例**共用，应当改成按实例区分。

**结论：不阻塞**，但重启后验收时如果看到"别的实例的账号"，那就是这个原因，不是 bug。

---

## 三、还未验证的（需要重启 DSH + 新建会话）

**端到端验收**：真实新会话是否真的拿到属于自己的邮箱账号。

```powershell
& "E:\zcz\modle\MCP\mcp-agent-mailbox\.venv\Scripts\python.exe" -X utf8 `
  "E:\zcz\modle\MCP\mcp-agent-mailbox\dsh-plugin\verify_after_restart.py"
```

期望：V1 PASS（出现 `native_session_id == 该会话 DSH_SESSION_ID` 的账号）、V4 PASS（旧写死账号不再占活跃连接）。

### 连带未验证的最后一个技术环节

`ctx.loader.import` 在**真实运行时**能否穿透 asar 取到 `@deepseek-ai/dsh-mcp-client`。
目前只有代码证据：DSH 给 root include 装了 `HostResolvedRootInclude`，把裸包名交给
`internal.import(spec, bareModuleBaseUrl)`（`dsh-app-boot/lib/index.js:3692-3701`），而
`bareModuleBaseUrl` 是**启动器**传的、插件拿不到。因此插件实现了三级回退
（loader 裸名 → loader 绝对 file URL → createRequire 候选目录），但**哪一级会在真机命中尚未观测**。

**若重启后失败**，落点会直接给出三级各自的失败原因；备选方案是把该包从 asar 物化到
profile 的 `node_modules`（需写 profile 目录的批准）。

---

## 四、本会话沙箱的限制（不是仓库缺陷）

**pytest 无法在本会话运行**：最小复现（空仓库 + 一个 `tmp_path` 用例）也会失败——

```
File ".../_pytest/pathlib.py", line 357, in cleanup_dead_symlinks
    for left_dir in root.iterdir():
PermissionError: [WinError 5] 拒绝访问。: '...\pytest-min'
deny: Everyone DeleteSubdirectoriesAndFiles（Deny 从父目录继承）
```

pytest 在自己的 basetemp 下创建的目录会被施加**不可列举**的权限。`.` 受影响的是
**所有使用 `tmp_path` 的仓库测试**（`test_repositories.py` / `test_mailbox_acceptance.py` /
`test_mcp_tools.py` 等）；`tests/test_domain.py`、`tests/test_adapters.py` 这类不用 tmp 的正常。

因此：
- C 的回归用 `E:\zcz\.scratch-mailbox-tests\c_verify.py`（独立进程 + 隔离库）验证，**未**落进仓库 `tests/`；
- 把 C 的用例搬进仓库 `tests/` 这件事**被这个沙箱限制挡住**，不是遗漏。

---

## 五、复跑命令一览

```powershell
$py = "E:\zcz\modle\MCP\mcp-agent-mailbox\.venv\Scripts\python.exe"
$node = "E:\Ai\node.exe"      # 或任意 Node >= 20.6
cd E:\zcz\modle\MCP\mcp-agent-mailbox\dsh-plugin

& $node --check index.js                 # 模块级语法
& $py -X utf8 preflight.py               # 装载前预检（20 项）
& $node _selftest/selftest.mjs           # 离线自检（23 项）
& $node _evidence/verify/verify.mjs      # 真实 cordis 独立复核
& $py -X utf8 migrate_profile.py --check # profile 迁移状态（只读）
& $py -X utf8 verify_after_restart.py    # 端到端验收（需重启后跑）
& $py -X utf8 E:\zcz\.scratch-mailbox-tests\c_verify.py   # C 的身份越权回归（8 项）
```

## 六、回滚

```powershell
Copy-Item "C:\Users\mxz\.dsh\profiles\desktop\cordis.patch.yml.bak-20261009-143439" `
          "C:\Users\mxz\.dsh\profiles\desktop\cordis.patch.yml" -Force
```
