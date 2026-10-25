# 小鲸鱼云端 QQ 机器人

云端使用 AstrBot 连接 DeepSeek，SnowLuma 负责 QQ 与 OneBot 消息连接，内置 noVNC 提供 QQ 扫码桌面。本地 Windows 管理程序负责 SSH 隧道、后台入口、服务器状态和运行日志。

部署文件和程序就绪不等于聊天验证完成。首次使用还需由账号持有人确认页面上的用户协议、扫码登录机器人 QQ，并用管理员 QQ 实际发送消息验证。聊天人格可在 AstrBot 后台设置；本项目不会把“界面能够打开”当作“QQ 已上线”或“自动回复成功”。

## 日常一键启动

在项目目录双击 `cloud/start-manager.bat`，或使用桌面的“小鲸鱼云端管理”快捷方式。启动器使用项目现有的 `.venv\Scripts\python.exe`，管理程序仅依赖 Python 标准库，无需额外安装面板依赖。

需要重新生成快捷方式时，在项目根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\cloud\create-shortcut.ps1
```

程序检查本地端口、建立 SSH 隧道后自动打开管理面板。保持启动器的控制台窗口打开；关闭这个窗口，或在其中按 `Ctrl+C`，会结束管理程序及其 SSH 子进程。**关闭浏览器标签页不会断开隧道。云端服务独立运行，本地断开后仍会继续工作。**

启动报端口占用时，先关闭已有管理窗口。程序不会终止其他占用端口的进程。SSH 中断后面板会显示断开状态，不会静默重连；关闭启动窗口再双击快捷方式即可重新连接。

无需自动打开浏览器时，也可在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe .\cloud\manager.py --no-browser
```

## 本机访问地址

默认地址如下，实际端口以私有 `cloud/local.json` 为准。所有入口都绑定本机回环地址，隧道建立后才能访问。

| 本机地址 | 用途 | 云端目标端口 |
| --- | --- | --- |
| `http://127.0.0.1:18780/` | 本地统一管理面板：后台入口、状态、日志 | 本地 Python 服务 |
| `http://127.0.0.1:16185/` | AstrBot：模型、聊天人格、插件、消息平台 | `127.0.0.1:6185` |
| `http://127.0.0.1:15099/` | SnowLuma：QQ 账号和 OneBot 连接 | `127.0.0.1:5099` |
| `http://127.0.0.1:16081/` | noVNC：云端 QQ 桌面、扫码登录 | `127.0.0.1:6081` |

面板同时提供“打开后台”和“页内查看”；后台不允许嵌入时使用新标签页。云端 Compose 也只将三个管理端口映射到 `127.0.0.1`，不需要将它们加入公网安全组。SSH 使用已有主机指纹，指纹异常会停止连接，需要先核对服务器身份。

## 第一次登录与聊天验证

1. 启动本地管理程序，打开 AstrBot。初始用户名是 `astrbot`；初始密码保存在私有 `cloud/credentials.local.json` 的 `astrbot_password` 字段。首次登录按后台提示修改密码，后续使用新密码。
2. 打开 SnowLuma，初始密码对应同一私有文件的 `snowluma_password`。阅读页面展示的用户协议、许可和授权说明，由账号持有人自行确认。
3. 打开 QQ 云端桌面，连接密码对应 `novnc_password`。在云端 QQ 界面显示二维码后，用准备登录服务器的**机器人 QQ 小号**扫码，并在手机上完成必要确认。不要用管理员大号替代小号登录云端。
4. 在管理面板确认“QQ 登录状态”和“AstrBot 消息连接”均已连接，再用配置的**管理员 QQ**向机器人小号发送普通文字，检查真实收发。初始化配置仅允许指定管理员的好友私聊；其他好友或群聊需要在 AstrBot 后台明确加入允许名单。
5. 在 AstrBot 后台选择 DeepSeek 模型，按需设置“大肥鱼”聊天人格，可复制本目录的 `persona.txt`。查看回复内容与运行日志，确认消息经过 QQ、OneBot 和 DeepSeek 完成一次往返。

初始密码文件还包含 API Key 和 OneBot Token，仅在本机编辑器中按字段查看，勿将整个文件粘贴到聊天、终端日志或 GitHub。更改密码后请妥善保存新密码：初始密码文件不会自动反映后台的修改。

SnowLuma 状态采集需要使用 `/opt/jingjing/credentials.json` 中的 `snowluma_password` 登录其状态接口；如果在后台改了 SnowLuma 密码，请同时私下更新远端该字段和本地留存值，否则面板可能显示“待登录确认”，但不代表机器人一定离线。noVNC 密码来自云端 `.env` 的 `VNC_PASSWD`；若需修改，应同步私有凭据并重新创建 SnowLuma 容器，不要只修改本地文件。

## 状态与日志

面板每 5 秒读取缓存状态，展示服务器名称、系统、开机时长、CPU 核数和一分钟负载、内存与磁盘使用情况。CPU 负载不是 CPU 使用率。机器人卡片展示机器人 QQ、管理员 QQ、模型、QQ 登录状态及 OneBot 连接状态。

AstrBot 与 SnowLuma 日志分别显示最近的运行记录，状态采集对已知密码、API Key 和 Token 做隐藏处理。日志仍可能包含聊天内容或账号信息，分享前请自行查看。SSH 或状态读取失败时，面板标记旧数据和上次成功同步时间，避免将旧状态当作当前状态。noVNC 随 SnowLuma 容器运行；未提供独立 noVNC 状态时，其运行状态复用 SnowLuma 容器状态，不能单独证明 QQ 已登录。

状态接口是 `http://127.0.0.1:18780/api/status`。也可从项目根目录只读查看云端状态：

```powershell
.\.venv\Scripts\python.exe .\cloud\deploy.py status
```

## 私有配置与文件位置

| 位置 | 内容 |
| --- | --- |
| 本地 `cloud/local.json` | 服务器、SSH 用户、私钥路径、本地端口、机器人及管理员设置 |
| 本地 `cloud/credentials.local.json` | 部署初始密码、DeepSeek Key、OneBot Token |
| 本地 SSH 私钥 | 由 `local.json` 指定，放在仓库外的个人 SSH 目录 |
| 云端 `/opt/jingjing/compose.yaml` | 容器部署定义，版本和镜像摘要固定 |
| 云端 `/opt/jingjing/.env`、`credentials.json`、`bot.json` | 私有环境、凭据、账号配置 |
| 云端 `/opt/jingjing/astrbot/data` | AstrBot 配置、模型及应用数据 |
| 云端 `/opt/jingjing/snowluma` | SnowLuma 配置、QQ 登录态与账号数据 |
| 云端 `/opt/jingjing/bin/status.py` | 只读状态采集脚本 |

本地配置、凭据、运行文件、根目录 `.env` 与私钥都不应提交 Git。仓库已忽略 `cloud/local.json`、`cloud/credentials.local.json` 和 `cloud/runtime/`；私钥始终保存在仓库外。账号登录态与备份也属于私有数据。

如果变更管理员或模型，应通过 AstrBot 后台更新实际运行设置，并同步私有部署记录。`initialize.py` 仅初始化不存在的文件，重新运行不会覆盖已有应用配置。

## 运维与备份

以下是运维命令示例。将 `SERVER_IP`、`SSH_USER`、`PRIVATE_KEY_PATH` 替换为私有配置中的值；不要复制示例占位符直接执行。正常使用面板无需另开这个 SSH 终端。

```powershell
ssh -i "PRIVATE_KEY_PATH" SSH_USER@SERVER_IP
```

进入服务器后，查看状态、日志和重启单个服务：

```bash
sudo docker compose --env-file /opt/jingjing/.env -f /opt/jingjing/compose.yaml ps
sudo docker compose --env-file /opt/jingjing/.env -f /opt/jingjing/compose.yaml logs --tail 80 astrbot
sudo docker compose --env-file /opt/jingjing/.env -f /opt/jingjing/compose.yaml logs --tail 80 snowluma
sudo docker compose --env-file /opt/jingjing/.env -f /opt/jingjing/compose.yaml restart astrbot
```

确实需要停止云端机器人时才执行 `stop`；随后使用 `start` 恢复，数据目录会保留：

```bash
sudo docker compose --env-file /opt/jingjing/.env -f /opt/jingjing/compose.yaml stop
sudo docker compose --env-file /opt/jingjing/.env -f /opt/jingjing/compose.yaml start
```

完整备份可在低峰时短暂停止服务后打包 `/opt/jingjing`，以保留一致的配置和账号数据。下面命令在**服务器终端**运行；无论打包是否成功，最后都执行 `start` 恢复服务，并检查打包命令的退出结果：

```bash
sudo install -d -m 700 /var/backups/jingjing
sudo docker compose --env-file /opt/jingjing/.env -f /opt/jingjing/compose.yaml stop
sudo sh -c 'umask 077; tar -C /opt -czf "/var/backups/jingjing/jingjing-$(date +%Y%m%d-%H%M%S).tar.gz" jingjing'
sudo docker compose --env-file /opt/jingjing/.env -f /opt/jingjing/compose.yaml start
sudo ls -lh /var/backups/jingjing
```

备份包含密码、Key、聊天数据和 QQ 登录态，应保留私有访问权限并另存一份；不要上传公开仓库。恢复时先停止服务、保留当前目录备份，再在相同路径恢复完整目录。镜像本身不包含在上述数据备份内，部署版本可由 `compose.yaml` 重新获取。

## 换服务器或重新部署

现有服务器不需要重复运行部署初始化。新服务器需先具备 Linux、Docker Engine、Docker Compose 插件，以及可使用密钥登录并执行 `sudo -n` 的 SSH 用户；首次 SSH 主机指纹应核对并记录。本地根目录先准备有效的 DeepSeek `.env` 和应用配置。

`deploy.py prepare` 会生成新的私有连接配置和随机密码，已有 `local.json` 或 `credentials.local.json` 时会拒绝覆盖；写入前会限制文件权限。以下命令中的占位符均需替换：

```powershell
.\.venv\Scripts\python.exe .\cloud\deploy.py prepare --host SERVER_IP --user SSH_USER --key "PRIVATE_KEY_PATH" --bot-qq BOT_QQ --admin-qq ADMIN_QQ
.\.venv\Scripts\python.exe .\cloud\deploy.py upload
```

`upload` 将文件通过 SSH 标准输入上传到 `/opt/jingjing`，已有部署时会拒绝覆盖。上传后先在服务器按固定摘要获取镜像：

```bash
sudo docker compose --env-file /opt/jingjing/.env -f /opt/jingjing/compose.yaml pull
```

然后回到本地项目根目录启动：

```powershell
.\.venv\Scripts\python.exe .\cloud\deploy.py start
.\cloud\start-manager.bat
```

`start` 使用 `--pull never`，因此必须先下载好镜像。`download-images.sh` 是首次下载的可选辅助脚本，仅适用于没有现有容器的服务器；已有服务不应为了下载而运行它。升级前先备份，检查新版本兼容性，再明确修改镜像版本及摘要，不自动追随 `latest`。
