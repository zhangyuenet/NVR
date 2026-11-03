"""
报警片段加密异地备份（Frigate → rclone crypt → OneDrive）
- 定期找出已结束的"报警"事件，导出录像片段 + 快照 + 元数据（含 SHA-256）
- 用 rclone 加密上传到远端（远端只看到乱码）
- 远端总量超过上限时，从最早的片段开始删除
- 运行状态写入 backup_status.json，供健康日报读取
"""
import os, json, time, hashlib, subprocess, shutil, traceback
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

FRIGATE = os.environ.get("FRIGATE_URL", "http://frigate:5000")
TZ = ZoneInfo(os.environ.get("TZ", "America/Toronto"))
REMOTE = os.environ.get("BACKUP_REMOTE", "onedrive-crypt:").rstrip("/") + "/"
QUOTA_BYTES = int(float(os.environ.get("BACKUP_QUOTA_GB", "5")) * 1024**3)
INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "300"))        # 每 5 分钟检查一次
LOOKBACK_H = int(os.environ.get("BACKUP_LOOKBACK_HOURS", "24"))  # 每次回看的时间范围
SETTLE = int(os.environ.get("BACKUP_SETTLE", "90"))              # 事件结束后等多久再导出（等录像落盘）
PAD = int(os.environ.get("BACKUP_PAD", "5"))                     # 片段前后各多录几秒
MAX_CLIP = int(os.environ.get("BACKUP_MAX_CLIP", "300"))         # 单个片段最长秒数
CAMERAS = {c.strip() for c in os.environ.get("BACKUP_CAMERAS", "").split(",") if c.strip()}  # 为空=全部

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "backup_state.json")
STATUS_FILE = os.path.join(HERE, "backup_status.json")
STAGING = os.environ.get("BACKUP_STAGING", "/tmp/frigate-backup")


def log(msg):
    print(f"[{datetime.now(TZ):%m-%d %H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def rclone(*args, timeout=1800):
    cmd = ["rclone", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"rclone {args[0]} 失败: {r.stderr.strip()[-500:]}")
    return r.stdout


# ============ 导出 ============
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export_review(rv):
    """导出一个报警事件到暂存目录，返回导出的字节数。"""
    cam, rid = rv["camera"], rv["id"]
    start = float(rv["start_time"]) - PAD
    end = min(float(rv["end_time"]) + PAD, start + MAX_CLIP)
    t = datetime.fromtimestamp(rv["start_time"], TZ)
    day_dir = os.path.join(STAGING, t.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    base = os.path.join(day_dir, f"{t:%Y%m%d-%H%M%S}_{cam}_{rid.split('-')[-1]}")

    # 录像片段（Frigate 按时间段从录像中截取）
    r = requests.get(f"{FRIGATE}/api/{cam}/start/{start:.1f}/end/{end:.1f}/clip.mp4",
                     timeout=300, stream=True)
    r.raise_for_status()
    with open(base + ".mp4", "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    if os.path.getsize(base + ".mp4") < 1024:
        raise RuntimeError("片段为空，可能录像还没写入")

    # 快照（带识别框）
    snap_ok = False
    for eid in rv.get("data", {}).get("detections", []):
        try:
            s = requests.get(f"{FRIGATE}/api/events/{eid}/snapshot.jpg",
                             params={"bbox": 1, "timestamp": 1}, timeout=20)
            if s.ok and s.content:
                with open(base + ".jpg", "wb") as f:
                    f.write(s.content)
                snap_ok = True
                break
        except Exception:
            pass

    # 元数据 + 校验值（证明文件未被修改）
    meta = {
        "camera": cam,
        "review_id": rid,
        "start": datetime.fromtimestamp(start, TZ).isoformat(),
        "end": datetime.fromtimestamp(end, TZ).isoformat(),
        "objects": rv.get("data", {}).get("objects", []),
        "zones": rv.get("data", {}).get("zones", []),
        "exported_at": datetime.now(TZ).isoformat(),
        "sha256": {"mp4": sha256(base + ".mp4")},
    }
    if snap_ok:
        meta["sha256"]["jpg"] = sha256(base + ".jpg")
    with open(base + ".json", "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    return sum(os.path.getsize(base + ext) for ext in (".mp4", ".jpg", ".json")
               if os.path.exists(base + ext))


def pending_reviews(done):
    now = time.time()
    items = requests.get(f"{FRIGATE}/api/review",
                         params={"after": now - LOOKBACK_H * 3600, "before": now,
                                 "severity": "alert", "limit": 500}, timeout=30).json()
    out = []
    for rv in items if isinstance(items, list) else []:
        if rv.get("severity") != "alert" or rv["id"] in done:
            continue
        if CAMERAS and rv.get("camera") not in CAMERAS:
            continue
        if not rv.get("end_time") or now - float(rv["end_time"]) < SETTLE:
            continue  # 事件还没结束，或录像还没落盘
        out.append(rv)
    return sorted(out, key=lambda r: r["start_time"])


# ============ 容量控制 ============
def enforce_quota():
    """远端超过上限时，按时间从早到晚删除。返回 (删除前总量, 删除后总量, 删除的片段数)。"""
    files = json.loads(rclone("lsjson", "-R", "--files-only", REMOTE) or "[]")
    files.sort(key=lambda f: f["Path"])  # 路径以 日期/时间 开头，排序即时间顺序
    total = sum(f["Size"] for f in files)
    before = total

    # 同一事件的 mp4/jpg/json 一起删
    groups = {}
    for f in files:
        groups.setdefault(f["Path"].rsplit(".", 1)[0], []).append(f)
    to_delete, removed = [], 0
    for key in sorted(groups):
        if total <= QUOTA_BYTES:
            break
        for f in groups[key]:
            to_delete.append(f["Path"])
            total -= f["Size"]
        removed += 1

    if to_delete:
        lst = os.path.join(STAGING, "_delete.txt")
        os.makedirs(STAGING, exist_ok=True)
        with open(lst, "w") as f:
            f.write("\n".join(to_delete))
        rclone("delete", REMOTE, "--files-from", lst)
        os.remove(lst)
        try:
            rclone("rmdirs", REMOTE, "--leave-root")
        except Exception:
            pass
        log(f"容量控制：删除 {removed} 个最早的片段，{before / 1024**3:.2f} GB → {total / 1024**3:.2f} GB")
    return before, total, removed


# ============ 主循环 ============
def run_once(state, status):
    done = state.setdefault("done", [])
    reviews = pending_reviews(set(done))
    exported, nbytes, errors = [], 0, []

    for rv in reviews:
        try:
            nbytes += export_review(rv)
            exported.append(rv["id"])
        except Exception as e:
            errors.append(f"{rv['camera']} {rv['id']}: {e}")
            log(f"[warn] 导出失败 {rv['id']}: {e}")

    uploaded = len(exported)
    if uploaded:
        # 上传成功后才标记为已完成；上传失败会抛出异常，下一轮重新导出
        rclone("move", STAGING, REMOTE, "--delete-empty-src-dirs",
               "--exclude", "_delete.txt", "--retries", "3")
        log(f"已上传 {uploaded} 个报警片段，{nbytes / 1024**2:.1f} MB")
        done.extend(exported)
        state["done"] = done[-5000:]
        save_json(STATE_FILE, state)

    _, used, removed = enforce_quota()

    # 上传记录：[时间戳, 片段数, MB]，保留 48 小时，供日报统计"过去 24 小时"
    now = time.time()
    log_ = [e for e in status.get("uploads", []) if e[0] >= now - 48 * 3600]
    if uploaded:
        log_.append([now, uploaded, round(nbytes / 1024**2, 1)])
    status["uploads"] = log_
    for k in ("day", "today_uploaded", "today_mb"):  # 旧版字段，不再使用
        status.pop(k, None)
    status.update({
        "last_ok": datetime.now(TZ).isoformat(timespec="seconds"),
        "remote_used_gb": round(used / 1024**3, 2),
        "quota_gb": round(QUOTA_BYTES / 1024**3, 1),
        "last_removed": removed,
        "last_error": errors[-1] if errors else None,
    })
    save_json(STATUS_FILE, status)


def main():
    log(f"异地备份启动：远端 {REMOTE}，上限 {QUOTA_BYTES / 1024**3:.1f} GB，每 {INTERVAL} 秒检查")
    state = load_json(STATE_FILE, {})
    status = load_json(STATUS_FILE, {})
    while True:
        try:
            run_once(state, status)
        except Exception as e:
            log(traceback.format_exc())
            status["last_error"] = f"{datetime.now(TZ):%m-%d %H:%M} {e}"[:300]
            save_json(STATUS_FILE, status)
        shutil.rmtree(STAGING, ignore_errors=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
