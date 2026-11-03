# 家庭本地 NVR 部署指南：Wyze V3 + Frigate

最后更新：2026-10-28

5 台 Wyze Cam v3 通过官方 RTSP 直连 Frigate，录像写入 NAS 保留 14 天；人车识别、AI 描述后推送到手机，报警片段加密备份到 OneDrive。本文是可复用的部署记录，配套代码见同一仓库。

## 架构

摄像头把视频流直接推给 Frigate；Frigate 负责录像和识别，事件通过 MQTT 交给三个小服务：推送、日报、备份。所有组件都跑在一台 Ubuntu 主机的 Docker 里（本例是 Windows 上的 Hyper-V 虚拟机）。

```text
Wyze Cam v3 ×5 ──RTSP──► Frigate ──── 录像 ────► NAS（CIFS 共享，14 天）
                            │
                            │ MQTT（mosquitto，只在 Docker 内部）
                            ├──► notifier  报警 → AI 复核 → Pushover（含声音报警）
                            ├──► health    离线/空间告警 + 每日日报
                            └──► backup    报警片段 → rclone 加密 → OneDrive

手机 / Mac ──Tailscale──► Frigate 网页（不对公网开放任何端口）
```

| 仓库文件 | 用途 |
| --- | --- |
| `docker-compose.example.yml` | 五个容器：frigate、mosquitto、notifier、health、backup，外加 NAS 录像卷 |
| `.env.example` | 所有密钥的模板；NAS、RTSP、Pushover、Claude API |
| `frigate/config.example.yml` | Frigate 配置模板（区域坐标是占位，需在网页里重画） |
| `notifier/notify.py` | 报警推送、AI 复核、车牌、声音报警 |
| `notifier/health.py` | 健康监控与每日日报 |
| `notifier/backup.py` | 报警片段加密异地备份 |
| `notifier/household.example.json` | 家庭车牌、摄像头位置描述的模板（真实文件不提交） |
| `docs/backup-recovery.md` | 从 OneDrive 找回录像的步骤 |

`.env`、`household.json`、`rclone.conf`、Frigate 的 `config.yml` 和数据库都在 `.gitignore` 里，不会上传。

## 第1步：NAS、虚拟机与网络准备

录像写到 NAS，程序和数据库留在主机本地；主机、NAS、摄像头都要在同一个主网络里，并各自固定 IP。

**NAS（本例 Synology DS220j，只做存储）**

1. 建共享文件夹 `frigate`，**关闭回收站**。Frigate 每天删除过期录像，回收站开着会在几周内堆满硬盘。
2. 建专用用户 `frigate`，只给这个文件夹读写权限。
3. 给这个用户设配额。按实测每天约 24 GB、5 台摄像头，14 天约 340 GB；配额 1.2 TB 留有充足余量。
4. SMB 最小协议设为 SMB2，最大 SMB3。

DS220j 是 ARM 处理器、512 MB 内存，跑不了 Docker，所以计算全部放在另一台电脑上。WD40EFAX 是 SMR 硬盘，录像写入压力不大，只是换盘重建会很慢；将来换盘时选 CMR 盘。

**主机：Windows 10 专业版 + Hyper-V 虚拟机**

1. 开启 Hyper-V，新建“外部”虚拟交换机，绑定有线网卡（桥接到局域网）。
2. 新建第 2 代虚拟机：内存 6 GB、处理器 4–6 个、动态磁盘 40 GB，安装 Ubuntu Server 24.04 LTS（勾选 OpenSSH）。
3. 安全启动模板改为 **Microsoft UEFI Certificate Authority**，否则 Ubuntu 启动不了。
4. 网卡 MAC 地址设为静态；自动启动操作选“始终自动启动”，自动停止选“关闭来宾操作系统”。
5. 在虚拟机里安装 Docker：

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
sudo apt install -y cifs-utils
```

**为什么不用 Docker Desktop**：Windows 10 上它跑在 WSL2 里，隔着一层地址转换，host 网络模式也穿不透。另外 Hyper-V 无法把核显交给 Linux 虚拟机，所以本例的视频解码是 CPU 软解。

**网络**

- 摄像头必须连在**主网络**。访客网络与主网络隔离，Frigate 连不到上面的设备。
- 在路由器上给虚拟机、NAS、每台摄像头设 DHCP 预留。
- 户外摄像头对 Wi-Fi 信号很敏感：在路由器 App 里看每台的连接质量，“Weak”的先解决信号，再谈其他调优。见“问题与经验记录”。

## 第2步：摄像头开启 RTSP

Wyze Cam v3 的官方固件已经支持 RTSP，Frigate 直接拉流，不再需要 wyze-bridge、Wyze API Key 和云端账号。**一定用不加密的 RTSP（554 端口），不要用 RTSPS（322 端口）**：本例信号较弱的摄像头用 RTSPS 时每 30–40 秒断一次，改成 RTSP 后不再断线。

1. Wyze App → 摄像头 → 设置 → 高级设置 → **RTSP** → Set Up，设用户名和密码。所有摄像头用同一组账号，写进 `.env` 的 `FRIGATE_RTSP_USER` / `FRIGATE_RTSP_PASSWORD`。
2. **Secure RTSP (RTSPS)** 关闭。地址格式：`rtsp://用户名:密码@摄像头IP:554/stream0`（主码流），`stream1` 是子码流。
3. **关闭摄像头固件自动更新**。有版本的固件更新曾清掉 RTSP 设置或引入断流问题，稳定后就固定版本。
4. 关闭摄像头自己的事件录制、移动侦测和推送，给它减负。Wyze App 可以隐藏起来，只用来改摄像头设置。
5. 先用 VLC 打开 RTSP 地址确认有画面，再写进 Frigate。

摄像头的 RTSP 流带音频（pcm\_alaw，8 kHz 单声道），可以做声音识别。初代 Wyze 门铃（WYZEDB3）没有 RTSP，目前接不进来，用前门的 V3 覆盖门口。

## 第3步：部署 Frigate、MQTT 与通知服务

把仓库克隆到主机，填好三个私密文件，再用一条命令启动全部容器。

```bash
git clone <你的仓库地址> ~/nvr && cd ~/nvr
cp .env.example .env && chmod 600 .env            # 填 NAS、RTSP、Pushover、Claude 密钥
cp docker-compose.example.yml docker-compose.yml
cp frigate/config.example.yml frigate/config.yml   # 改摄像头 IP
cp notifier/household.example.json notifier/household.json   # 填家庭车牌（可选）
docker compose up -d frigate mosquitto notifier health
```

验证：

1. `docker logs frigate` 里找到首次启动打印的 admin 初始密码。
2. `docker exec frigate df -h /media/frigate` 显示的应该是 NAS 配额的大小。**如果显示的是虚拟机磁盘大小，说明 NAS 没挂上，录像正写进本地磁盘，先处理。**
3. NAS 的 `frigate` 文件夹里出现 `recordings` 目录。日期和小时文件夹按 UTC 时间命名，是正常的。

**开机时 NAS 还没准备好**：主机比 NAS 启动得快时，CIFS 卷会挂载失败、容器起不来。加一条 cron 每 5 分钟拉起一次：

```bash
*/5 * * * * cd ~/nvr && /usr/bin/docker compose up -d >/dev/null 2>&1
```

**改配置后怎么生效**：

- 只改 `frigate/config.yml` → 在 Frigate 网页的配置编辑器里“保存并重启”即可。
- 改了 `docker-compose.yml` 或 `.env` → `docker compose up -d --force-recreate <服务名>`；`restart` 不会读取新的环境变量。
- 改了 `notifier/` 里的 Python 脚本 → `docker compose restart notifier`（或 health、backup）。脚本是挂载进去的，不重启容器就还在跑旧代码。
- **每次重启 Frigate，都会丢掉所有物体的跟踪状态**，停着的车可能被当成新车再报一次警。重启后几分钟内的报警不必理会。

**MQTT 安全**：mosquitto 允许匿名连接，所以**不要给它映射端口**。否则局域网里任何设备都能发 `frigate/profile/set` 这类消息，关掉识别或切换布防状态。

## 第4步：识别模型（OpenVINO + YOLOv9）

在 Intel CPU 上，把识别器从默认的 `cpu` 换成 `openvino`，是投入最小、收益最大的一步。模型不变、只换识别器，本例（i7-4790、3 路识别）的实测结果：

| 指标 | CPU 识别器 | OpenVINO |
| --- | --- | --- |
| 推理时间 | 26.85 ms | 16.37 ms |
| 识别器 CPU 占用 | 245.7% | 67.8% |
| 整体 CPU 占用 | 84% | 40% |

**再进一步：换成 YOLOv9-t（320）**。内置的 MobileNet 模型容易把门窗、玩偶当成人，对远处的小目标和动物也弱。YOLOv9 模型不随 Frigate 提供，要用官方脚本导出一次（约 10–20 分钟，临时占用 5–8 GB 磁盘）：

```bash
mkdir -p ~/nvr/frigate/model_cache && cd ~/nvr/frigate/model_cache
docker build . --build-arg MODEL_SIZE=t --build-arg IMG_SIZE=320 --output . -f- <<'EOF'
FROM python:3.11 AS build
RUN apt-get update && apt-get install --no-install-recommends -y cmake libgl1 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.0 /uv /bin/
WORKDIR /yolov9
ADD https://github.com/WongKinYiu/yolov9.git .
RUN uv pip install --system -r requirements.txt
RUN uv pip install --system onnx==1.18.0 onnxruntime onnx-simplifier==0.4.36 onnxscript
ARG MODEL_SIZE
ARG IMG_SIZE
ADD https://github.com/WongKinYiu/yolov9/releases/download/v0.1/yolov9-${MODEL_SIZE}-converted.pt yolov9-${MODEL_SIZE}.pt
RUN sed -i "s/ckpt = torch.load(attempt_download(w), map_location='cpu')/ckpt = torch.load(attempt_download(w), map_location='cpu', weights_only=False)/g" models/experimental.py
RUN python3 export.py --weights ./yolov9-${MODEL_SIZE}.pt --imgsz=${IMG_SIZE} --simplify --include onnx
FROM scratch
ARG MODEL_SIZE
ARG IMG_SIZE
COPY --from=build /yolov9/yolov9-${MODEL_SIZE}.onnx /yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx
EOF
docker builder prune -f    # 清理构建缓存
```

然后把 `config.yml` 的 `model:` 换成模板里的 YOLOv9 写法（`model_type: yolo-generic`，320×320，`labelmap_path: /labelmap/coco-80.txt`）。效果不满意时，模板里保留了回退到内置模型的写法。

- 320 不吃亏：Frigate 会先裁出有运动的区域再放大识别，对远处小目标反而更好。
- 换模型后，得分分布会变化，先观察几天再调阈值。本例换用 YOLOv9 后，俯拍侧门的人从约 60–70% 提到了 95%。
- 新模型能看见以前看不见的远处路人。如果换模型后突然多了路人报警，要检查区域是否画得太靠近人行道。
- 模型只认识 COCO 的 80 类：没有浣熊、松鼠，浣熊常被识别成 cat、dog 或 bear。

## 第5步：区域、遮罩与过滤

误报绝大多数靠“位置”解决，不靠调分数。本例实测：万圣节米老鼠装饰和快递员的得分（60–73% 对 60–70%）和宽高比（0.66–0.88 对 0.61–0.76）完全重叠，提高阈值只会漏掉快递员。

**先理解 Frigate 怎么判断位置**：用的是识别框**底边中点**（物体“脚踩的位置”），不是整个框。在 Explore 里打开一次误报，画面上的彩色点就是它。

| 工具 | 作用 | 本例怎么用 |
| --- | --- | --- |
| 区域（Zone）+ `required_zones` | 只有进入区域才算报警，其余只记为“检测” | 车道区域上边缘离人行道留出明显距离；侧门区域只画地砖和台阶，不包括门板和墙 |
| `inertia` | 连续 N 帧都在区域内才算进入，过滤识别框抖动 | 车道 5 |
| `loitering_time` | 在区域内停留 N 秒才触发 | 车道 5 秒：横穿路口的路人不报警 |
| 物体遮罩（Object Mask） | 底边中点落在里面的该类物体一律忽略 | 开门侧的假人装饰：只盖住它底边中点落下的那一小块，不盖住门垫中间 |
| 运动遮罩（Motion Mask） | 忽略这块区域的画面变化 | 后院只盖树冠和天空，不盖地面 |
| `min_area` | 小于画面一定比例的框忽略 | 后院动物 0.005（0.5%），过滤松鼠，按 Explore 里的 Area 实际数据调 |
| 只跟踪需要的类别 | 每多一类就多一种误报 | 前门不跟踪 dog：人离摄像头太近、框占画面 44% 时会被识别成狗 |
| `review.alerts.labels` | 哪些类别算报警（默认只有 person、car） | 后院加上 cat、dog、bear，让浣熊也能报警 |

两个容易误判的情况：

- **一条报警包含多个物体**。Explore 里看到的只是其中一个的轨迹；路人报警可能是另一个并排走的人踩进了区域。看轨迹记录里有没有“entered …\_zone”。
- **光影变化触发停着的车**。树影、车灯扫过会让停着的车被重新识别；重启 Frigate 后尤其常见。如果不需要车辆推送，可以把 `review.alerts.labels` 设为只有 person。

## 第6步：通知（Pushover + AI 复核）

`notifier` 订阅 MQTT 的 `frigate/reviews`，**只推送 Alert（报警），不推送 Detection（检测）**。每条推送带一张有识别框的快照、一句中文描述和“查看回放”链接。

**一条报警的处理顺序**

1. 去重，同一摄像头 60 秒内只推一次。
2. **选快照**：从这条报警的所有物体里，去掉比报警早 2 分钟以上出现的“老物体”（早就停着的车），再按“在报警区域内 → 人优先 → 得分”排序。没有新物体时，用当前实时画面。
3. **AI 复核**：把无框的快照发给 Claude（默认 `claude-haiku-5-5`），返回 `real`、一句话描述、车牌。`real` 为 false（装饰、反光、松鼠等）就不推送。
4. **车牌**：AI 读到的车牌和 `household.json` 里的家庭车辆对上时，标题改为称呼（如“Driveway：家人的车”）；看不清的字符用 `?` 代替。
5. AI 调用失败或超时 → 照常推送简单描述。宁可多报，不可漏报。

**配置**：`.env` 里填 `PUSHOVER_TOKEN`、`PUSHOVER_USER`、`PUBLIC_URL`，要用 AI 再填 `CLAUDE_API_KEY`。Claude API 按量计费，和 Claude.ai 订阅分开充值。家人的手机装 Pushover 登录同一账号即可收到推送。

**已知局限**

- **延迟**：从进入区域到收到通知约 20–30 秒，主要是 `loitering_time`、等快照生成（每次 2 秒，最多 3 次）和 AI 调用。
- **没有上下文**：AI 只看一张图，同一辆车两次分别读出 “ABC?” 和 “?BCD” 也拼不起来，更认不出家人。这要靠 Frigate 自带的车牌识别（LPR）和人脸识别，见“待办与路线图”。
- **隐私**：快照会发到外部 API。不想外发可以不填 `CLAUDE_API_KEY`，将来换成本地视觉模型时只需改调用地址。
- 夜间红外画面是黑白的，颜色、性别、车牌的准确度都会下降。

## 第7步：健康日报、声音识别与异地备份

三项功能都是为“出事那天一定有录像”服务的：系统挂了要知道，摄像头看不到的声音要听到，设备被偷了录像还在。

**健康监控与日报（`health.py`）**

| 情况 | 推送 |
| --- | --- |
| 摄像头超过 5 分钟没有画面 | 离线告警（高优先级），恢复后再推一条 |
| Frigate 连不上 | 告警 |
| 录像目录不在 NAS（挂载类型不是 cifs） | 告警，防止录像悄悄写满本地磁盘 |
| 录像空间超过 90% | 每天最多提醒一次 |
| 每天 08:00 | 日报：在线数、断线、过去 24 小时报警数、空间、备份状态 |

日报本身就是心跳：哪天没收到，就是整套系统出了问题。日报里的所有统计都用“过去 24 小时”，不用“今天”：早上 8 点的“今天”几乎总是 0。

**声音识别**

1. 摄像头输入的 `roles` 加上 `audio`，全局 `audio.listen` 列出要识别的声音。故意不列 `speech`：每个说话的路人都会触发。
2. `notify.py` 订阅 `frigate/+/audio/+`：烟雾报警、火警、玻璃破碎、尖叫按高优先级推送，汽车警报按普通优先级；同一摄像头同一声音 5 分钟内只推一次。
3. 想让录像带声音，记录预设用 `preset-record-generic-audio-aac`。
4. 测试：在摄像头旁用手机播放烟雾报警声。

加拿大录制他人私人对话有法律风险。声音**识别**不保存声音，可以全开；录像**录音**建议只在室内、离家时使用。

**报警片段加密异地备份（`backup.py`）**

- 每 5 分钟导出已结束的报警：`.mp4`（前后各多 5 秒，最长 5 分钟）、带框快照、含 SHA-256 校验值的 `.json`。
- 用 rclone 的 crypt 层在本地加密后上传到 OneDrive，云端只看得到乱码。
- 云端上限 5 GB，超过就从最早的片段开始删。本例每天约 20–40 次报警、每次 4–8 MB，大约能保留 2–4 周。
- 配置步骤：先 `rclone config` 建 `onedrive`（无浏览器的主机选“不自动授权”，在 Mac 上运行 `rclone authorize "onedrive"`），再建 `onedrive-crypt`（类型 crypt，remote 填 `onedrive:FrigateBackup`，两个密码都选随机生成并**保存到密码管理器**）。
- Mac 上的 OneDrive 客户端要取消同步 `FrigateBackup` 文件夹。
- 找回录像的完整步骤在 `docs/backup-recovery.md`。这份说明不要只放在家里的设备上。

## 第8步：远程访问（Tailscale）

不在路由器上开放任何端口。主机和家人的手机都装 Tailscale，用 Tailscale Serve 把 Frigate 发布成一个只有自己网络内才能访问的 HTTPS 地址。

```bash
sudo tailscale serve --bg https+insecure://localhost:8971
```

- 主机在 Tailscale 管理后台关闭密钥过期（Disable key expiry），否则半年后会掉线。
- iPhone 的 Tailscale 打开“按需连接”（VPN On Demand），点推送里的链接时不用手动连 VPN。
- 给家人开 viewer 角色的账号，只能看、不能改配置和删录像。在 Safari 里“添加到主屏幕”，用起来像一个 App。

**可选：免密登录**。普通用户点推送链接时要输密码，登录后还会跳回首页而不是对应录像。Tailscale Serve 会在每个请求里带上访问者的 `Tailscale-User-Login`（并删掉伪造的同名头部），Frigate 的代理认证可以直接用它作身份：

1. `docker-compose.yml` 里 Frigate 的端口写 `"127.0.0.1:8971:8971"`。**这是安全前提**：关掉 Frigate 登录后，谁能直连 8971，谁就能伪造身份。代价是局域网 IP 访问不了，在家也用 Tailscale 地址。
2. `config.yml` 写入模板里注释掉的 `auth` / `proxy` 两段：`header_map` 的 user 和 role 都用 `tailscale-user-login`，`role_map` 把管理员的登录邮箱映射为 admin，其他人默认 viewer。
3. 家人要用**自己的 Tailscale 账号**加入网络（免费版支持 3 个用户）。用你的账号登录的设备，会被识别成你（管理员）。

## 验证清单与日常维护

部署完按下面逐项验证，确认无人照看时也能持续录像。

- [ ] 重启 Windows 不登录：虚拟机、全部容器自动运行，录像继续写入 NAS
- [ ] 拔掉一台摄像头电源 5 分钟：收到离线告警，插回后收到恢复通知
- [ ] 在每个区域里走一遍：收到带正确快照的推送；在人行道上走过：不推送
- [ ] 在摄像头旁播放烟雾报警声：收到声音报警
- [ ] 第二天 08:00 收到日报，备份行显示有上传
- [ ] 照 `docs/backup-recovery.md` 从 OneDrive 下载并解密一个片段，核对 SHA-256
- [ ] 用家人的手机点一条推送里的“查看回放”，确认能打开

**日常维护**

- 每天看一眼日报；出现 ⚠️ 再登录处理。
- 每月在项目目录运行 `docker compose pull && docker compose up -d` 更新镜像。Frigate 大版本升级前先看更新说明，配置格式可能变化。
- 某台摄像头经常断线：先看 `docker logs frigate` 里是不是 `tls` 报错（RTSPS 的问题），再看 Wi-Fi 信号。
- 统计各摄像头 24 小时内的重连次数：

```bash
for c in driveway frontdoor sidedoor backyard livingroom; do
  echo -n "$c: "; docker logs frigate --since 24h 2>&1 | grep -E "$c.*Restarting ffmpeg" | wc -l
done
```

- 留证据：在 Frigate 里对相关时段用“导出”（Export），导出文件不受保留天数影响。万圣节、圣诞节的装饰收起后，记得删掉为它们画的物体遮罩。

## 问题与经验记录

部署以来遇到的问题，按时间倒序。最大的三个教训：**网络和信号决定一切**；**误报靠位置解决，不靠分数**；**改完代码要确认真的在运行新版本**。

| 日期 | 现象 | 原因 | 解决 |
| --- | --- | --- | --- |
| 10-28 | 后院想报浣熊、不报松鼠 | 模型没有浣熊类别；AI 提示词只认人和车，会把动物判成误报 | 跟踪 cat/dog/bear + `min_area` 过滤小框；提示词加入中大型动物，小动物算误报 |
| 10-27 | 推送图片是 1 小时前的画面，AI 描述也跟着错 | 一条报警包含早就停着的车；脚本取了列表里第一个物体的快照，那是它很久以前的最佳画面 | 跳过比报警早 2 分钟以上出现的物体，按区域、类别、得分选图 |
| 10-27 | 日报“今日上传 0 个片段” | 日报在 08:00 发，“今天”刚开始 | 改为统计过去 24 小时 |
| 10-26 | 家人点推送链接要输密码，登录后跳到首页 | Frigate 登录会丢掉原链接 | Tailscale 身份头 + Frigate 代理认证，8971 只听本机 |
| 10-23 | AI 报“未返回 JSON”，推送退回简单描述 | `max_tokens` 300 太小，回答被截断 | 改为 2048；日志打印 `stop_reason` |
| 10-23 | 站在前门正下方的人被识别成狗（95%） | 离镜头太近、俯拍，框占画面 44%，形状像四足动物 | 前门不再跟踪 dog；确实需要就加 `max_area` |
| 10-23 | 更新脚本后没有生效 | 脚本是挂载进容器的，`up -d --build` 见配置未变就不重启 | `docker compose restart notifier`；日志里的新字段可用来确认版本 |
| 10-23 | `ffprobe ... \| grep Stream` 没有输出 | 报错被 grep 过滤掉；占位符未替换；容器内没有 ffprobe 的默认路径 | 去掉 grep，在容器内用环境变量拼地址 |
| 10-22 | 换 YOLOv9 后，几乎每组路人都报警 | 新模型能看见远处路人；车道区域上边缘离人行道只差约 1% 画面高度 | 区域上边缘下拉，`inertia` 5，`loitering_time` 5 秒 |
| 10-22 | 侧门门板被识别成人（60%），当时没人 | 阳光在玻璃上反光，瘦高的门窗像人 | 侧门加区域，只画地砖和台阶 |
| 10-22 | 前门万圣节米老鼠装饰反复报人 | 得分和宽高比都和真人重叠；第一版遮罩没盖到底边中点 | 物体遮罩只盖住装饰底边中点落下的位置；不动阈值 |
| 10-22 | 停着的车被报成“进入车道” | 重启 Frigate 丢掉跟踪状态；光影变化让车被重新识别 | 知道重启后会有一次；快照选择跳过老物体 |
| 10-22 | 识别占用 CPU 245%，整机 84% | 默认 CPU 识别器效率低 | 换 OpenVINO 识别器，整机降到 40% |
| 10-22 | 车道摄像头每 30–40 秒断一次，日志全是 `tls` 报错 | RTSPS 在弱信号下撑不住 | 改用 RTSP（554），立刻稳定 |
| 10-21 | 电力猫做有线回程，网速反而变慢 | 家中电线实测只有 29.5 Mbps | 退货；保持无线回程，将来考虑升级 Wi-Fi 7 Mesh |
| 10-21 | 三台户外摄像头频繁断线（6 小时内 100+ 次） | Wi-Fi 信号弱；Mesh 自带的“点间测试”只测回程，不反映摄像头信号 | 增加 Mesh 点、调整位置；以设备连接质量和 Frigate 断线次数为准 |
| 10-20 | iPhone 上回放放不出来 | wyze-bridge 输出的 H.264 不标准 | 改为摄像头官方 RTSP 直连，停用 bridge |
| 10-19 | 开机后 Frigate 起不来 | 主机比 NAS 启动快，CIFS 卷挂载失败 | cron 每 5 分钟 `docker compose up -d` |
| 10-19 | 摄像头连不上 | 摄像头在访客网络，和主机隔离 | 摄像头移到主网络 |
| 10-19 | Docker Desktop 下摄像头发现超时 | WSL2 的地址转换挡住了 UDP 回包 | 改用 Hyper-V 桥接虚拟机 |

## 硬件建议

认车牌、认家人靠的是小模型（Frigate 的 LPR、人脸识别），不需要显卡；只有“用一句话描述场景”的本地视觉大模型才需要。所以分两档，价格为 2026-10-24 的公开报价。

| 档位 | 硬件 | 参考价格 | 能做什么 | 注意 |
| --- | --- | --- | --- | --- |
| 标准版 | Intel N150 迷你主机，16 GB 内存，带 2.5 寸硬盘位 | 约 120–200 美元 | 4–6 路录像、OpenVINO 识别、硬件解码、LPR、人脸识别；功耗 10–15 W | 原生 Linux，不用虚拟机；录像接 NAS 或加一块 2 TB 硬盘 |
| AI 版 A | Mac mini（24 GB 更稳妥） | 基础款 16 GB 约 1,249 加元（含税约 1,411 加元） | 本地跑 Qwen3-VL 8B 描述场景；安静、省电 | Frigate 要跑在 Docker 里，没有视频硬解；视觉模型要在 macOS 原生运行 |
| AI 版 B | 台式机 + RTX 5060 Ti 16 GB | 显卡当时最低约 1,120 加元（近 12 个月中位数约 665 加元） | CUDA 生态最成熟，Frigate、Ollama 装上就能用 | 显卡价格偏高时可以等 |

**比主机更重要的是摄像头和供电**

- 商业或新部署优先选**有线 PoE、带子码流、原生 RTSP** 的摄像头：一根网线供电和传数据，没有 Wi-Fi 断线问题，识别用子码流也更省 CPU。
- 主机、NAS、路由器接 UPS；BIOS 里打开“来电自动开机”。
- NAS 硬盘选 CMR 盘（如 WD Red Plus、Seagate IronWolf），不要用 SMR 盘。
- 本例的旧平台（i7-4790、16 GB，Hyper-V 虚拟机 6 GB）跑 5 路录像 + 3–4 路识别，CPU 约 40%，够用。

## 待办与路线图

按优先级排列；前五项是当前部署与本文推荐做法之间的差距。

- [ ] 前门、后院、客厅三台仍用 RTSPS（322），改成 RTSP（554）
- [ ] 确认识别模型：当前 `config.yml` 里仍是内置 MobileNet，按第4步换回 YOLOv9
- [ ] 后院开启识别后画运动遮罩（树冠、天空），几天后按 Explore 里的 Area 校准 `min_area`
- [ ] 实施 Tailscale 免密登录（第8步），家人用自己的 Tailscale 账号
- [ ] `rclone.conf` 与 `docs/backup-recovery.md` 各存一份在家外（密码管理器或云端普通文件夹）
- [ ] 开启 Frigate 自带的车牌识别（LPR，`known_plates` + `match_distance`）和人脸识别：多帧拼出完整车牌、认出家人；已识别的家人和家庭车辆直接推送，不再把照片发给外部 AI
- [ ] 两段式推送：先发快速通知，AI 描述好了再发详细版；在脚本里记录各阶段耗时
- [ ] 一键切换在家/出门/旅行：Siri 快捷指令 → 本地接口 → MQTT `frigate/profile/set`；旅行模式用 python-kasa 在局域网内随机开关灯
- [ ] 本地视觉模型（Qwen3-VL 8B）替代 Claude API，照片不出家门
- [ ] iOS App：APNs 推送替代 Pushover，推送内播片段、Face ID、一键切换模式

## 参考资料

- [Frigate 文档：Profiles](https://docs.frigate.video/configuration/profiles/) 与 [MQTT（frigate/profile/set）](https://docs.frigate.video/integrations/mqtt/)
- [Frigate 文档：Authentication（代理认证）](https://docs.frigate.video/configuration/authentication)
- [Frigate 文档：License Plate Recognition](https://docs.frigate.video/configuration/license_plate_recognition)
- [Frigate 部署指南：YOLOv9 导出与 320 尺寸的取舍（Core Lab）](https://corelab.tech/setupfrigate/)
- [Tailscale Serve（身份头部）](https://tailscale.com/docs/features/tailscale-serve.md)
- [RTX 5060 Ti 16GB 加拿大价格走势（GPUPrix）](https://gpuprix.com/gpus/geforce-rtx-5060-ti-16gb)
- [Mac mini 加拿大价格（The Mac Index）](https://v4.themacindex.com/ca/products/mac-mini)
- [本地视觉模型对比（InsiderLLM）](https://insiderllm.com/guides/vision-models-locally/)
