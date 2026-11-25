# TagAI 趋势代币导入服务

每轮从 GMGN 的 Trending 和 Hot Search 读取 BSC 与 Robinhood 的 1 分钟、5 分钟、1 小时、6 小时和 24 小时榜单。默认每榜取前 50 个，按合约去重并筛选市值超过 30 万美元的代币。服务优先恢复未完成的导入，再执行交易池评分、模拟及注册。输出 JSON 包含各链的快照、恢复和导入结果。

## 在 Windows 上安装

准备 Git、Python 3.13（带 py 启动器）和 Node.js 22（带 npm）。在 PowerShell 运行：

~~~powershell
git clone https://github.com/tagai-dao/tagagent-trending-import.git
cd tagagent-trending-import
.\install.ps1
~~~

安装程序创建独立 Python 环境，并为官方钱包应用显式选链补丁。首次使用时，在 wallet 目录运行钱包自带的安装程序，按提示完成本机授权。已有兼容钱包可在项目根目录的 .env 中设置 TRENDING_IMPORT_WALLET_DIR 为其绝对路径。

~~~powershell
cd wallet
.\setup.ps1
cd ..
~~~

在项目根目录的 .env 中填写自己的 GMGN_API_KEY。先保持 TRENDING_IMPORT_EXECUTE=0，运行一轮预检：

~~~powershell
.\.venv\Scripts\python.exe run.py
~~~

查看输出中的 status、stages 和各链的 candidate_details。预检也需要钱包地址和可访问的链 RPC。准备好各链手续费、核对单笔金额及每日次数上限后，在 .env 设置 TRENDING_IMPORT_EXECUTE=1，用同一命令正式运行。Claw 钱包的授权信息由钱包在本机管理。

## 让 Windows Codex 定时运行

在 Codex 中打开这个仓库，发送以下指令：

> 创建每 10 分钟运行一次的本地自动化。从本仓库根目录用 .venv/Scripts/python.exe 执行 run.py 一次。按链报告快照、恢复、尝试数和完整 status_counts。列出新增导入的代币、合约、交易哈希，以及需要处理的问题。无变化时静默。流动性或永久资格过滤属于正常结果。

运行时保持电脑唤醒、Codex 和钱包沙箱可用。多个进程通过 runtime/locks 下的文件锁互斥。每个钱包使用一份持久化 runtime 目录，升级时保留。迁移同一钱包前先停止旧实例，并通过私密渠道迁移台账，其中可能包含已签名的交易。可选的 FOMO 回退需要外部采集器提供新鲜快照，可通过 TRENDING_IMPORT_FOMO_SNAPSHOT 指定路径。所有配置默认值见 tagagent/config.py。
