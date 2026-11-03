"""
Frigate 健康监控与日报
- 摄像头离线 / 恢复 即时告警
- Frigate 无法访问告警
- 录像目录不在 NAS（CIFS）上、或空间不足告警
- 每天定时推送一份日报（同时也是"心跳"：哪天没收到，说明系统出问题了）
"""
import os, json, time, traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

# ============ 设置 ============
TOKEN = os.environ["PUSHOVER_TOKEN"]
USER = os.environ["PUSHOVER_USER"]
FRIGATE = os.environ.get("FRIGATE_URL", "http://frigate:5000")
TZ = ZoneInfo(os.environ.get("TZ", "America/Toronto"))

REPORT_TIME = os.environ.get("REPORT_TIME", "08:00")          # 日报时间
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))  # 巡检间隔（秒）
OFFLINE_MINUTES = int(os.environ.get("OFFLINE_MINUTES", "5")) # 离线多久才告警
DISK_WARN_PCT = float(os.environ.get("DISK_WARN_PCT", "90"))  # 空间告警阈值
REC_PATH = "/media/frigate/recordings"
EXPECTED_MOUNT = os.environ.get("EXPECTED_MOUNT", "cifs")     # 录像应挂载在 NAS 上

# 不做"离线告警"的摄像头（例如故意关掉的），逗号分隔
IGNORE_CAMS = {c.strip() for c in os.environ.get("IGNORE_CAMS", "").split(",") if c.strip()}

# ============ 状态 ============
offline_since = {}      # cam -> 首次发现离线的时间
offline_alerted = set() # 已发过离线告警的摄像头
outage_log = []         # (cam, 开始, 结束)  —— 用于日报
frigate_down_since = None
frigate_down_alerted = False
disk_warned_day = None
mount_warned = False
last_report_day = None


def log(msg):
    print(f"[{datetime.now(TZ):%m-%d %H:%M:%S}] {msg}", flush=True)


def push(title, msg, priority=0):
    try:
        r = requests.post("https://api.pushover.net/1/messages.json",
                          data={"token": TOKEN, "user": USER, "title": title,
                                "message": msg, "priority": priority}, timeout=20)
        log(f"[push] {title} -> {r.status_code}")
    except Exception as e:
        log(f"[push] 失败: {e}")


def get(path, **params):
    r = requests.get(f"{FRIGATE}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def fmt_dur(seconds):
    m = int(seconds // 60)
    return f"{m // 60}小时{m % 60}分钟" if m >= 60 else f"{m}分钟"


def fmt_size(mib):
    return f"{mib / 1024 / 1024:.2f} TB" if mib >= 1024 * 1024 else f"{mib / 1024:.1f} GB"


# ============ 巡检 ============
def check_cameras(stats):
    now = time.time()
    cams = stats.get("cameras", {})
    for cam, s in cams.items():
        if cam in IGNORE_CAMS:
            continue
        online = (s.get("camera_fps") or 0) >= 1
        if not online:
            offline_since.setdefault(cam, now)
            dur = now - offline_since[cam]
            if dur >= OFFLINE_MINUTES * 60 and cam not in offline_alerted:
                offline_alerted.add(cam)
                push(f"⚠️ {cam} 离线", f"{cam} 已经 {fmt_dur(dur)} 没有画面，录像可能中断。", priority=1)
        elif cam in offline_since:
            start = offline_since.pop(cam)
            dur = now - start
            if dur >= 60:  # 短于 1 分钟的抖动不记录
                outage_log.append((cam, start, now))
            if cam in offline_alerted:
                offline_alerted.discard(cam)
                push(f"✅ {cam} 已恢复", f"离线时长 {fmt_dur(dur)}。")


def check_storage(stats):
    global disk_warned_day, mount_warned
    st = stats.get("service", {}).get("storage", {}).get(REC_PATH)
    if not st:
        return
    total, used = st.get("total", 0), st.get("used", 0)
    mount = st.get("mount_type", "")
    pct = used / total * 100 if total else 0
    today = datetime.now(TZ).date()

    if EXPECTED_MOUNT and mount and mount != EXPECTED_MOUNT:
        if not mount_warned:
            mount_warned = True
            push("⚠️ 录像未写入 NAS",
                 f"录像目录当前挂载类型是 {mount}，不是 {EXPECTED_MOUNT}。"
                 f"NAS 可能没挂上，录像正写到虚拟机本地磁盘，空间很快会满。", priority=1)
    elif mount_warned and mount == EXPECTED_MOUNT:
        mount_warned = False
        push("✅ NAS 已重新挂载", "录像已恢复写入 NAS。")

    if pct >= DISK_WARN_PCT and disk_warned_day != today:
        disk_warned_day = today
        push("⚠️ 录像空间不足", f"已用 {pct:.0f}%（{fmt_size(used)} / {fmt_size(total)}）。")


def check_once():
    global frigate_down_since, frigate_down_alerted
    try:
        stats = get("/api/stats")
    except Exception as e:
        now = time.time()
        frigate_down_since = frigate_down_since or now
        if now - frigate_down_since >= OFFLINE_MINUTES * 60 and not frigate_down_alerted:
            frigate_down_alerted = True
            push("🚨 Frigate 无法访问",
                 f"已经 {fmt_dur(now - frigate_down_since)} 无法连接 Frigate，所有摄像头可能都没有录像。\n{e}",
                 priority=1)
        return None

    if frigate_down_alerted:
        push("✅ Frigate 已恢复", f"中断时长 {fmt_dur(time.time() - frigate_down_since)}。")
    frigate_down_since, frigate_down_alerted = None, False

    check_cameras(stats)
    check_storage(stats)
    return stats


# ============ 日报 ============
def count_reviews(after, before):
    """返回 {camera: {"alert": n, "detection": n}}"""
    counts = {}
    try:
        items = get("/api/review", after=after, before=before, limit=2000)
        for it in items if isinstance(items, list) else []:
            c = counts.setdefault(it.get("camera", "?"), {"alert": 0, "detection": 0})
            sev = it.get("severity")
            if sev in c:
                c[sev] += 1
    except Exception as e:
        log(f"[report] 读取事件失败: {e}")
    return counts


BACKUP_STATUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup_status.json")


def backup_line():
    """读取 backup.py 写的状态文件；没有部署备份时返回 None。"""
    try:
        with open(BACKUP_STATUS) as f:
            st = json.load(f)
    except Exception:
        return None
    try:
        last = datetime.fromisoformat(st["last_ok"])
        age_h = (datetime.now(TZ) - last).total_seconds() / 3600
    except Exception:
        return "⚠️ 异地备份：从未成功运行"
    cutoff = time.time() - 86400
    recent = [e for e in st.get("uploads", []) if e[0] >= cutoff]
    n = sum(e[1] for e in recent)
    mb = sum(e[2] for e in recent)
    line = (f"异地备份：云端 {st.get('remote_used_gb', 0)} / {st.get('quota_gb', 0)} GB，"
            f"过去 24 小时上传 {n} 个片段" + (f"（{mb:.0f} MB）" if n else ""))
    if age_h > 1:
        line = f"⚠️ 异地备份已 {age_h:.0f} 小时未成功运行！" + (f"\n错误：{st['last_error']}" if st.get("last_error") else "")
    elif st.get("last_error"):
        line += f"\n⚠️ 最近错误：{st['last_error']}"
    return line


def build_report(stats):
    now = time.time()
    after = now - 86400
    lines = []

    # 摄像头状态
    cams = stats.get("cameras", {}) if stats else {}
    online = [c for c, s in cams.items() if (s.get("camera_fps") or 0) >= 1]
    offline = [c for c in cams if c not in online]
    if stats is None:
        lines.append("🚨 Frigate 当前无法访问！")
    elif offline:
        lines.append(f"⚠️ 摄像头 {len(online)}/{len(cams)} 在线，离线：{', '.join(offline)}")
    else:
        lines.append(f"✅ 摄像头 {len(cams)}/{len(cams)} 全部在线")

    # 过去 24 小时的离线记录（含仍在离线的）
    outages = [(c, s, e) for c, s, e in outage_log if e >= after]
    outages += [(c, s, now) for c, s in offline_since.items() if now - s >= 60]
    if outages:
        per = {}
        for c, s, e in outages:
            n, t = per.get(c, (0, 0))
            per[c] = (n + 1, t + (e - max(s, after)))
        lines.append("断线：" + "；".join(f"{c} {n}次/共{fmt_dur(t)}" for c, (n, t) in per.items()))
    else:
        lines.append("过去 24 小时无断线")

    # 事件统计
    counts = count_reviews(after, now)
    if counts:
        total_a = sum(v["alert"] for v in counts.values())
        total_d = sum(v["detection"] for v in counts.values())
        detail = "，".join(f"{c} {v['alert']}" for c, v in sorted(counts.items()) if v["alert"])
        lines.append(f"报警 {total_a} 次" + (f"（{detail}）" if detail else "") + f"，检测 {total_d} 次")
    else:
        lines.append("过去 24 小时无事件")

    # 存储
    if stats:
        st = stats.get("service", {}).get("storage", {}).get(REC_PATH, {})
        total, used, free = st.get("total", 0), st.get("used", 0), st.get("free", 0)
        if total:
            line = f"录像空间 {fmt_size(used)} / {fmt_size(total)}（{used / total * 100:.0f}%）"
            try:
                usage = get("/api/recordings/storage")
                per_hour = sum((v.get("bandwidth") or 0) for v in usage.values())  # MiB/小时
                if per_hour > 0:
                    line += f"，每天约 {fmt_size(per_hour * 24)}"
            except Exception:
                pass
            lines.append(line)
            if st.get("mount_type") and EXPECTED_MOUNT and st["mount_type"] != EXPECTED_MOUNT:
                lines.append(f"⚠️ 录像目录挂载类型为 {st['mount_type']}，不在 NAS 上！")

        bk = backup_line()
        if bk:
            lines.append(bk)

        up = stats.get("service", {}).get("uptime")
        ver = stats.get("service", {}).get("version", "")
        if up:
            lines.append(f"Frigate {ver} 已连续运行 {up // 86400} 天")

    return "\n".join(lines)


def maybe_report(stats):
    global last_report_day, outage_log
    now = datetime.now(TZ)
    hh, mm = map(int, REPORT_TIME.split(":"))
    if now.date() != last_report_day and (now.hour, now.minute) >= (hh, mm):
        last_report_day = now.date()
        problem = stats is None or any(
            (s.get("camera_fps") or 0) < 1 for c, s in stats.get("cameras", {}).items() if c not in IGNORE_CAMS)
        push(("⚠️ " if problem else "📋 ") + f"监控日报 {now:%m-%d}", build_report(stats),
             priority=0 if problem else -1)
        cutoff = time.time() - 86400 * 2
        outage_log = [o for o in outage_log if o[2] >= cutoff]


# ============ 主循环 ============
def main():
    global last_report_day
    # 启动时如果已经过了今天的日报时间，就不补发，等明天
    now = datetime.now(TZ)
    hh, mm = map(int, REPORT_TIME.split(":"))
    if (now.hour, now.minute) >= (hh, mm):
        last_report_day = now.date()
    log(f"健康监控启动：每 {CHECK_INTERVAL} 秒巡检，离线 {OFFLINE_MINUTES} 分钟告警，日报时间 {REPORT_TIME}")
    push("健康监控已启动", f"每天 {REPORT_TIME} 推送日报。")

    while True:
        try:
            stats = check_once()
            maybe_report(stats)
        except Exception:
            log(traceback.format_exc())
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
