# 登录 handoff 离线修复与自审边界

本次是本地候选修复, 不发布版本, 不改变游戏交互安全边界。

## ok-nte fork

`BaseNTETask.wait_login` 在首次识别 `login_setting` 后累计登录预算: 最多 5 次进入游戏点击或 60 秒, 下一次轮询先检查是否已登录/已入队, 未成功则记录错误并抛出既有 `WaitFailedException`。模板暂时消失不重置预算, 成功或失败结束后清理状态, 允许后续独立尝试。`AutoLoginTask` 和每日流程共用此入口, 不新增另一套点击逻辑。

`tests/TestLoginWait.py` 隔离加载实际生产方法, 用 mock 时钟/识别/输入执行 7 个回归案例, 不加载 GUI 或连接游戏。它不覆盖 ok-script executor 如何处理异常, 因此不能据此保证宿主 exit code 或任务管理器的终态映射。

## YeYuGamer 独立改动

saved-account handoff 的拥有者是 YeYuGamer 的 `adapter-host/nte-runner/NteSavedLogin.py`, 不是本 fork。修复与测试在该仓库单独提交, 不将其代码、模板或提交混入本 fork。

YeYuGamer 本地审查提交为 `ad7e5e2c905cc7a41d3ab41ce90b107337d3670a`, 分支 `codex/nte-login-handoff-review-20261028`。该仓库的 runner、模板与原有测试此前未被 Git 跟踪, 此次将这些依赖和修复一起首次纳入版本控制; 没有纳入其他任务的改动或已暂存文件。

三张非隐私模板与现有候选缓存一致。旧窗口门硬编码 Qt 版本类名, 且先要求可见所属窗口总数为 1; 旧模板门要求 16:9 整窗和历史绝对 ROI。新实现保留可见性、直接 owner、进程名、官方输入锁和前台检查, 改为筛选唯一三模板相对布局, 按匹配按钮中心单次点击。相关系数下限仍为 0.97, 不选择账号、不输入凭据。

模板合成回放可证明这些代码限制及修复行为, 不能证明失败现场确实由哪一种布局变化引发。没有当前失败弹窗截图, 不将离线修复称为真实登录已通过。

## 验证

从本 fork 根目录运行:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p TestLoginWait.py -v
.\.venv\Scripts\python.exe <YeYuGamer>/adapter-host/tests/validate_nte_saved_login.py
.\.venv\Scripts\python.exe <YeYuGamer>/adapter-host/tests/validate_nte_saved_login_geometry.py
.\.venv\Scripts\python.exe -m py_compile src/tasks/BaseNTETask.py src/tasks/trigger/AutoLoginTask.py tests/TestLoginWait.py
git diff --check
```

`<YeYuGamer>` 表示维护源码仓库, 不是安装目录或候选缓存。当前虚拟环境仅准备离线测试依赖, 未安装完整 ok-script 运行依赖; 未执行完整框架/GUI/真实游戏测试。

实测结果: fork 登录上限 7/7, 原有 handoff 语义 14/14, 新增布局与所属窗口测试 10/10 通过。语法编译、针对新增/修改匹配代码及测试的 ruff、提交路径 whitespace 检查通过。新布局测试在旧实现上先失败, 再经修复转绿; 输入覆盖多种缩放、平移和非 16:9 裁剪, 以及缺模板、常量模板、重复布局、其他进程/owner 和无点击坐标的拒绝路径。

仅提交本地分支, 等待工具质检自审。未 merge、push、Install、修改 Manager 状态或启动日课。后续实机验收仍须经正式入口并核对同账号、同尝试的官方结果。
