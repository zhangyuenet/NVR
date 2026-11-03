# Frigate 报警片段异地备份：恢复说明

> 这份说明写给**很久以后、已经忘了这套系统怎么回事的自己**。需要找回录像时，从头按顺序看就行。

---

## 一、一句话说明

家里 Frigate 的**每一次报警**，都会被自动导出成视频片段，**在本地加密后**上传到 **OneDrive**，放在 `FrigateBackup` 文件夹里。在 OneDrive 网页或 Mac 上，看到的只是一堆乱码文件名，**直接打开是看不了的**，必须用 `rclone` 加上密码才能解密下载。

| 项目 | 内容 |
|---|---|
| 备份什么 | 只备份"报警"（Alert）事件，不备份 24 小时连续录像 |
| 每个事件包含 | `.mp4` 视频（事件前后各多 5 秒，最长 5 分钟）、`.jpg` 快照（带识别框）、`.json` 元数据（时间、摄像头、识别到的物体、SHA-256 校验值） |
| 存放位置 | OneDrive → `FrigateBackup` 文件夹（已加密） |
| 加密方式 | rclone crypt：文件内容、文件名、文件夹名全部加密 |
| 容量上限 | 5 GB，超过后自动删除最早的片段，大约能保留 2–4 周 |
| 运行方式 | NVR 主机上的 Docker 容器 `backup`，每 5 分钟检查一次 |
| 相关文件 | `notifier/backup.py`（备份脚本）、`docker-compose.yml`（`backup` 服务） |

**备份的用途**：家里的电脑或 NAS 被偷、被毁之后，从云端找回最近的报警录像。平时要查录像，直接在 Frigate 里看就行（NAS 上保留 14 天完整录像），**不需要用到这份备份**。

---

## 二、恢复前，必须准备好的东西

缺了下面任何一项，就**无法解密**，云端的文件等于废纸：

1. **Microsoft 账号**：OneDrive 所用的账号；
2. **两个加密密码**（password 和 password2/盐值）：创建 `onedrive-crypt` 时由 rclone 随机生成。
   保存位置：**＿＿＿＿＿＿＿＿＿＿＿＿（在这里写上你存密码的位置，例如"1Password 中的『Frigate 备份 rclone 密码』"）**

   或者使用：
3. **rclone 配置文件**的备份 `rclone.conf`：里面包含了 OneDrive 的连接信息和加密后的两个密码，有了它就不需要单独输入密码。
   原始位置：NVR 主机上的 `~/.config/rclone/rclone.conf`
   备份位置：**＿＿＿＿＿＿＿＿＿＿＿＿**

> ⚠️ 密码一旦丢失，**任何人（包括微软和我自己）都无法解密**。

---

## 三、情况 A：家里的 NVR 主机还在，能正常登录

这种情况最简单，所有东西都已经配置好了。

**1. 登录 NVR 主机**
```bash
ssh 用户名@NVR主机IP
```

**2. 查看有哪些日期的备份**
```bash
rclone lsd onedrive-crypt:
```
会列出 `2026-10-24` 这样的日期文件夹。

**3. 查看某一天的文件**
```bash
rclone ls onedrive-crypt:2026-10-24
```

**4. 下载（自动解密）**
```bash
# 下载某一天的全部片段
rclone copy onedrive-crypt:2026-10-24 ~/证据/2026-10-24 -P

# 只下载某个摄像头（例如 driveway）
rclone copy onedrive-crypt:2026-10-24 ~/证据/2026-10-24 --include "*_driveway_*" -P

# 只下载某个时间段（例如 14:00–14:59 的片段）
rclone copy onedrive-crypt:2026-10-24 ~/证据/2026-10-24 --include "20261024-14*" -P
```

**5. 拷贝到 Mac 上**（在 Mac 的终端里运行）
```bash
scp -r 用户名@NVR主机IP:~/证据 ~/Desktop/
```

---

## 四、情况 B：家里的设备都没了，只剩一台 Mac

**1. 在 Mac 上安装 rclone**
```bash
brew install rclone
```

**2a. 如果有 `rclone.conf` 的备份**（最快）
```bash
mkdir -p ~/.config/rclone
cp /备份位置/rclone.conf ~/.config/rclone/rclone.conf
rclone lsd onedrive-crypt:      # 能列出日期文件夹，就说明成功了
```
如果提示 token 过期或者需要授权，运行：
```bash
rclone config reconnect onedrive:
```
浏览器会弹出来，登录 Microsoft 账号授权就行。

**2b. 如果只有两个密码**，就重新配置（需要大约 5 分钟）：

运行 `rclone config`，然后：

先建 OneDrive 连接：
- `n`（新建）→ 名字输入 `onedrive` → 类型输入 `onedrive`
- client_id、client_secret：直接回车留空
- region：`global`
- Edit advanced config：`n`
- 使用浏览器自动授权：`y`（Mac 上有浏览器），登录 Microsoft 账号
- 选择 `OneDrive Personal`，选择你的盘，确认保存

再建加密层（**每一项都必须和当初完全一致**，否则无法解密）：
- `n`（新建）→ 名字输入 `onedrive-crypt` → 类型输入 `crypt`
- remote：`onedrive:FrigateBackup`
- filename_encryption：`standard`
- directory_name_encryption：`true`
- password：选 `y`（自己输入），**输入保存的第一个密码**
- password2：选 `y`，**输入保存的第二个密码（盐值）**
- Edit advanced config：`n` → `y` 确认 → `q` 退出

验证：
```bash
rclone lsd onedrive-crypt:
```
如果能列出日期文件夹，说明成功；如果报错，或者列出来的是乱码，说明密码或设置不对。

**3. 下载**：和情况 A 的第 4 步一样，只是把保存路径改成 Mac 上的，例如 `~/Desktop/证据`。

---

## 五、怎么看懂下载下来的文件

文件名格式：`日期-时间_摄像头_编号.后缀`，例如：

```
20261024-175152_driveway_a1b2c3.mp4    ← 视频（可以直接用 QuickTime 播放）
20261024-175152_driveway_a1b2c3.jpg    ← 快照（带识别框）
20261024-175152_driveway_a1b2c3.json   ← 元数据
```
- 时间是**本地时间**（由 `TZ` 设置决定），是事件开始的时间（视频还会再往前多录 5 秒）；
- 摄像头名：`driveway` 车道、`frontdoor` 前门、`sidedoor` 侧门、`backyard` 后院、`livingroom` 客厅。

`.json` 里的内容示例：
```json
{
 "camera": "driveway",
 "start": "2026-10-24T17:51:47-04:00",
 "end": "2026-10-24T17:52:27-04:00",
 "objects": ["person"],
 "zones": ["driveway_zone"],
 "sha256": {"mp4": "1984a5f9...", "jpg": "14fe0fd3..."}
}
```

---

## 六、作为证据提交（警察、保险公司）

**1. 校验文件没有被修改过**
```bash
shasum -a 256 ~/Desktop/证据/2026-10-24/*.mp4
```
把算出来的值和对应 `.json` 里的 `sha256.mp4` 比较，**完全一致**就说明视频从导出那一刻起没有被改动过。

**2. 提交时，把同一事件的三个文件一起交**（`.mp4` + `.jpg` + `.json`），并且说明：
- 视频由家庭监控系统（Frigate NVR）自动录制；
- 事件发生后几分钟内，自动导出并加密上传到云端；
- `.json` 里记录了导出时间和 SHA-256 校验值。

**3. 如果家里的设备还在**，也可以直接在 Frigate 网页里导出更完整的录像（任意时间段，不限于报警片段）：
打开 Frigate → Review（回放）→ 找到事件 → Export（导出）。

---

## 七、日常运维（平时用不到）

| 想做的事 | 命令（在 NVR 主机的项目目录下运行） |
|---|---|
| 看备份是否正常 | 每天早上的"监控日报"里有一行"异地备份：云端 X / 5 GB，今日上传 N 个片段"。如果出现 ⚠️，就说明出问题了 |
| 看备份日志 | `docker logs --tail 50 backup` |
| 重启备份服务 | `docker compose restart backup` |
| 查看云端总占用 | `rclone size onedrive-crypt:` |
| 修改容量上限 | 修改 `docker-compose.yml` 里 `backup` 服务的 `BACKUP_QUOTA_GB`，然后运行 `docker compose up -d backup` |
| OneDrive 授权过期 | `rclone config reconnect onedrive:`（需要在有浏览器的电脑上授权，见第四节） |

---

## 八、注意事项

- **不要**在 OneDrive 网页或 Mac 上手动删除、移动、重命名 `FrigateBackup` 里的文件。脚本是根据这些文件来统计容量、删除旧片段的；
- Mac 上已经设置为**不同步** `FrigateBackup` 文件夹（OneDrive 设置 → 账户 → 选择文件夹）。换了新 Mac 的话，记得重新设置一次；
- OneDrive 上被自动删除的旧片段，会先进入 OneDrive 回收站保留一段时间。所以如果刚过保留期，**可以去 OneDrive 回收站里找找看**（还原到原位置后，再用 rclone 下载解密）；
- 常见报错：
  - `"onedrive-crypt" refers to a local folder`：命令里**漏写了冒号**，应该是 `onedrive-crypt:`；
  - `directory not found`：日期文件夹不存在，用 `rclone lsd onedrive-crypt:` 看看有哪些日期；
  - `didn't find section in config file`：配置名字拼错了，用 `rclone config show` 查看现有的配置。
