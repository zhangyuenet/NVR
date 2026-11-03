import os, json, time, threading, base64, re
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import paho.mqtt.client as mqtt

# ============ 基本设置 ============
TOKEN = os.environ["PUSHOVER_TOKEN"]
USER = os.environ["PUSHOVER_USER"]
FRIGATE = os.environ.get("FRIGATE_URL", "http://frigate:5000")
PUBLIC = os.environ.get("PUBLIC_URL", "").rstrip("/")
COOLDOWN = int(os.environ.get("COOLDOWN", "60"))
TZ = ZoneInfo(os.environ.get("TZ", "America/Toronto"))
SNAPSHOT_WAIT = float(os.environ.get("SNAPSHOT_WAIT", "2"))  # 等待 Frigate 生成事件快照的秒数
STALE_SECONDS = int(os.environ.get("STALE_SECONDS", "120"))  # 比报警早出现这么久的物体，视为"老物体"

# ============ AI 复核设置（Claude API）============
CLAUDE_KEY = os.environ.get("CLAUDE_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
AI_ENABLED = os.environ.get("AI_ENABLED", "1") == "1" and bool(CLAUDE_KEY)
AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "25"))

# 家庭信息（车牌、摄像头位置描述）放在单独的 household.json 里，不进代码仓库。
# 格式见 household.example.json。文件不存在时，AI 照常工作，只是不认识家庭车辆。
HOUSEHOLD_FILE = os.environ.get(
    "HOUSEHOLD_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "household.json"))
try:
    with open(HOUSEHOLD_FILE, encoding="utf-8") as _f:
        _household = json.load(_f)
except FileNotFoundError:
    _household = {}

# 家庭车辆：车牌（只写字母和数字，大写）→ {"owner": 称呼, "desc": 外观描述}
KNOWN_CARS = {k.upper(): v for k, v in _household.get("cars", {}).items()}
# 摄像头位置说明，帮助 AI 理解画面
CAMERAS = _household.get("cameras", {})

LABELS = {"person": "人", "car": "车", "bicycle": "自行车", "dog": "动物", "cat": "动物", "bear": "动物"}

# ============ 声音报警 ============
# Frigate 声音标签 → (中文名称, Pushover 优先级：1=高优先级，0=普通)
AUDIO_LABELS = {
    "smoke_detector": ("烟雾报警器响了", 1),
    "fire_alarm": ("火警警报声", 1),
    "glass": ("玻璃声（可能破碎）", 1),
    "shatter": ("玻璃破碎声", 1),
    "scream": ("尖叫声", 1),
    "car_alarm": ("汽车防盗警报", 0),
    "siren": ("警笛声", 0),
    "yell": ("大声叫喊", 0),
    "bark": ("狗叫声", 0),
}
# 需要推送的声音标签（其余只记录在 Frigate 里），可用环境变量覆盖
AUDIO_PUSH = set(os.environ.get(
    "AUDIO_PUSH", "smoke_detector,fire_alarm,glass,shatter,scream,car_alarm").split(","))
AUDIO_COOLDOWN = int(os.environ.get("AUDIO_COOLDOWN", "300"))  # 同一摄像头同一声音 5 分钟内只推一次
audio_last = {}

last_sent = {}
sent_ids = set()
lock = threading.Lock()


# ============ 截图 ============
def fetch_event_image(det_ids, bbox):
    """取事件快照。bbox=1 带识别框（给人看），bbox=0 干净画面（给 AI 看）。"""
    for eid in det_ids:
        try:
            r = requests.get(
                f"{FRIGATE}/api/events/{eid}/snapshot.jpg",
                params={"bbox": bbox, "timestamp": 0, "crop": 0, "height": 720, "quality": 90},
                timeout=10,
            )
            if r.ok and r.content:
                return r.content, eid
        except Exception as e:
            print(f"[warn] 事件快照失败 {eid}: {e}", flush=True)
    return None, None


LABEL_PRIORITY = {"person": 3, "car": 2, "bicycle": 2, "motorcycle": 2}


def rank_detections(review):
    """
    一条报警会包含这段时间内所有被跟踪的物体，其中可能有早就停着不动的车。
    这些"老物体"的快照是它们很久以前的最佳画面，不能代表这次报警。
    这里按"是否新出现 → 是否在报警区域 → 类型（人优先）→ 得分"排序，
    并剔除比报警开始时间早太多的老物体。
    """
    det_ids = review.get("data", {}).get("detections", [])
    review_start = float(review.get("start_time") or time.time())
    review_zones = set(review.get("data", {}).get("zones", []))
    ranked = []
    for eid in det_ids:
        try:
            ev = requests.get(f"{FRIGATE}/api/events/{eid}", timeout=10).json()
        except Exception as e:
            print(f"[warn] 读取事件信息失败 {eid}: {e}", flush=True)
            continue
        age = review_start - float(ev.get("start_time") or review_start)
        if age > STALE_SECONDS:
            print(f"[snap] 跳过老物体 {ev.get('label')} {eid}（比报警早 {age / 60:.0f} 分钟出现）", flush=True)
            continue
        data = ev.get("data") or {}
        score = data.get("top_score") or ev.get("top_score") or data.get("score") or 0
        in_zone = bool(review_zones & set(ev.get("zones") or []))
        ranked.append(((in_zone, LABEL_PRIORITY.get(ev.get("label"), 1), score), eid))
    ranked.sort(reverse=True)
    return [eid for _, eid in ranked]


def get_snapshots(review):
    """返回 (带框图片, 干净图片, 来源说明)。"""
    cam = review["camera"]

    # 事件刚开始时快照可能还没生成，等一下再取，最多重试 3 次
    for _ in range(3):
        time.sleep(SNAPSHOT_WAIT)
        det_ids = rank_detections(review)
        boxed, eid = fetch_event_image(det_ids, 1)
        if boxed:
            clean, _ = fetch_event_image([eid], 0)
            return boxed, clean or boxed, f"event {eid}"
    # 找不到新物体的快照时，用当前实时画面，而不是用老物体的旧快照

    try:
        r = requests.get(f"{FRIGATE}/api/{cam}/latest.jpg", params={"h": 720}, timeout=10)
        if r.ok:
            return r.content, r.content, "latest"
    except Exception as e:
        print(f"[warn] 实时截图失败: {e}", flush=True)
    return None, None, "none"


# ============ AI 复核与描述 ============
def build_prompt(cam, what):
    cars = "；".join(f"{v['owner']}：{v['desc']}，车牌 {k[:4]}-{k[4:]}"
                    for k, v in KNOWN_CARS.items()) or "（未登记）"
    return f"""你是家庭安防摄像头的 AI 助手。这是"{CAMERAS.get(cam, cam)}"摄像头的事件截图，目标检测模型报告画面中有：{what}。

请完成以下任务：
1. 判断是否真有需要提醒的目标：真实的人、真实的车辆，或真实的中大型动物（浣熊、狐狸、郊狼、臭鼬、鹿、猫、狗等）。如果是装饰品、玩偶、雕像、阴影、反光、门窗等被误识别，real 为 false。松鼠、花栗鼠、鸟、兔子这类小动物，也算 real 为 false。
2. 用一句简体中文（不超过 40 个字）描述发生了什么：
   - 人：说明是成年男性、成年女性还是儿童，以及在做什么（走向门口、离开、在车道上走动等）。
   - 如果此人穿着快递制服、拿着或放下包裹、或旁边有快递车（Amazon、Canada Post、UPS、FedEx、Purolator、DHL 等），说明是快递员，并尽量说出是哪家。
   - 车：说明颜色、品牌和车型，尽量具体，例如"白色 Tesla Model Y"；不确定时加"疑似"。
   - 动物：说出具体是什么动物（检测模型标的"猫/狗/熊"往往不准，以你看到的为准），以及在做什么，例如"一只浣熊在翻后院的垃圾桶"。
   - 如果是夜间红外黑白画面，无法判断颜色，就不要描述颜色。
3. 只要画面中有车辆，就尽力识别车牌号（包括陌生车辆，以及只能看清部分字符的情况）：
   - 填写 plate：只填字母和数字，不加空格和横线；看不清的字符用 ? 代替，例如 "ABC?123"；完全看不清就填 null。
   - 不要猜测或编造看不到的字符。
   - summary 中不用重复车牌号，程序会自动附加。

家庭车辆：{cars}。
- 车牌与家庭车辆一致时，直接用称呼，例如"妻子的黑色 SUV 回来了"。
- 车牌看不清、但外观与家庭车辆一致时，用"疑似"，例如"疑似我的车"。

只输出 JSON，不要输出其他内容，格式：
{{"real": true, "summary": "一句话描述", "plate": null}}"""


def ai_review(img, cam, what):
    """调用 Claude API。返回 dict（real/summary/plate），失败时返回 None。"""
    if not AI_ENABLED or not img:
        return None
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": int(os.environ.get("AI_MAX_TOKENS", "2048")),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(img).decode()}},
                {"type": "text", "text": build_prompt(cam, what)},
            ],
        }],
    }
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body, timeout=AI_TIMEOUT,
        )
        if not r.ok:
            print(f"[ai] 请求失败 {r.status_code}: {r.text[:200]}", flush=True)
            return None
        resp = r.json()
        blocks = resp.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        m = re.search(r"\{.*\}", text, re.S)  # 只取 JSON 部分，防止模型多说话
        if not m:
            usage = resp.get("usage", {})
            print(f"[ai] 未返回完整 JSON（stop_reason={resp.get('stop_reason')}, "
                  f"output_tokens={usage.get('output_tokens')}）: {text[:200]}", flush=True)
            return None
        result = json.loads(m.group(0))
        return result if isinstance(result, dict) else None
    except Exception as e:
        print(f"[ai] 解析失败: {e}", flush=True)
        return None


def clean_plate(plate):
    """规范化车牌：只保留字母、数字和 ?。"""
    if not plate:
        return ""
    return re.sub(r"[^A-Z0-9?]", "", str(plate).upper())


def match_plate(plate):
    return KNOWN_CARS.get(clean_plate(plate))


def format_plate(p):
    """安省车牌常见格式 ABCD-123，4+3 位时加横线，便于阅读。"""
    return f"{p[:4]}-{p[4:]}" if len(p) == 7 else p


# ============ 推送 ============
def notify(review):
    cam, rid = review["camera"], review["id"]
    now = time.time()
    with lock:
        if rid in sent_ids:
            return
        if now - last_sent.get(cam, 0) < COOLDOWN:
            print(f"[skip] {cam} 冷却中: {rid}", flush=True)
            return
        # 先占位，防止同一事件在处理期间被重复推送
        prev_sent = last_sent.get(cam, 0)
        sent_ids.add(rid)
        last_sent[cam] = now

    data = review.get("data", {})
    objs = [LABELS.get(o.split("-")[0], o) for o in data.get("objects", [])]
    what = "、".join(dict.fromkeys(objs)) or "活动"
    when = datetime.fromtimestamp(review.get("start_time", now), TZ).strftime("%H:%M:%S")
    cam_name = cam.replace("_", " ").title()

    boxed, clean, source = get_snapshots(review)

    # AI 复核：判定为误报则不推送；AI 出错时照常推送（宁可多报，不可漏报）
    ai = ai_review(clean, cam, what)
    if ai is not None and ai.get("real") is False:
        print(f"[ai] {cam} 判定误报，不推送: {ai.get('summary', '')}", flush=True)
        with lock:
            last_sent[cam] = prev_sent  # 误报不占用冷却时间
        return

    summary = (ai or {}).get("summary") or f"检测到{what}"
    plate = clean_plate((ai or {}).get("plate"))
    car = match_plate(plate)
    title = f"{cam_name}：{car['owner']}" if car else f"{cam_name}：{what}"
    msg = f"{when} {summary}"
    if plate:
        msg += f"\n车牌：{format_plate(plate)}" + ("（家庭车辆）" if car else "")
    if ai is None and data.get("zones"):
        msg += f"（区域：{', '.join(data['zones'])}）"

    files = {"attachment": ("snapshot.jpg", boxed, "image/jpeg")} if boxed else None
    payload = {"token": TOKEN, "user": USER, "title": title, "message": msg}
    if PUBLIC:
        payload["url"] = f"{PUBLIC}/review?id={rid}"
        payload["url_title"] = "查看回放"
    try:
        r = requests.post("https://api.pushover.net/1/messages.json",
                          data=payload, files=files, timeout=20)
        ai_tag = "AI" if ai else "无AI"
        print(f"[send] {cam} [{ai_tag}] {summary} 图片={source} -> {r.status_code} {r.text[:80]}", flush=True)
        ok = r.ok
    except Exception as e:
        print(f"[error] 推送失败: {e}", flush=True)
        ok = False

    with lock:
        if not ok:
            # 推送失败则撤销占位，允许后续重试
            sent_ids.discard(rid)
            last_sent[cam] = prev_sent
        elif len(sent_ids) > 2000:
            sent_ids.clear()


def notify_audio(cam, label):
    """声音报警：附上当时的实时画面，不经过 AI。"""
    key = (cam, label)
    now = time.time()
    with lock:
        if now - audio_last.get(key, 0) < AUDIO_COOLDOWN:
            return
        audio_last[key] = now
    name, priority = AUDIO_LABELS[label]
    when = datetime.now(TZ).strftime("%H:%M:%S")
    files = None
    try:
        r = requests.get(f"{FRIGATE}/api/{cam}/latest.jpg", params={"h": 720}, timeout=10)
        if r.ok:
            files = {"attachment": ("snapshot.jpg", r.content, "image/jpeg")}
    except Exception as e:
        print(f"[warn] 截图失败: {e}", flush=True)
    payload = {"token": TOKEN, "user": USER, "priority": priority,
               "title": f"🔊 {cam.replace('_', ' ').title()}：{name}",
               "message": f"{when} 检测到{name}，请查看回放确认。"}
    if PUBLIC:
        payload["url"] = f"{PUBLIC}/review"
        payload["url_title"] = "查看回放"
    try:
        r = requests.post("https://api.pushover.net/1/messages.json",
                          data=payload, files=files, timeout=20)
        print(f"[audio] {cam} {label} -> {r.status_code}", flush=True)
    except Exception as e:
        print(f"[error] 声音推送失败: {e}", flush=True)


# ============ MQTT ============
def on_connect(client, userdata, flags, reason_code, properties):
    print(f"[mqtt] 已连接: {reason_code}", flush=True)
    print(f"[ai] {'已启用，模型 ' + CLAUDE_MODEL if AI_ENABLED else '未启用（缺少 CLAUDE_API_KEY）'}", flush=True)
    print(f"[audio] 推送的声音：{', '.join(sorted(AUDIO_PUSH))}", flush=True)
    client.subscribe("frigate/reviews")
    client.subscribe("frigate/+/audio/+")


def on_message(client, userdata, m):
    # 声音事件：frigate/<摄像头>/audio/<标签>，内容为 ON/OFF
    parts = m.topic.split("/")
    if len(parts) == 4 and parts[2] == "audio":
        cam, label = parts[1], parts[3]
        if label in AUDIO_LABELS and label in AUDIO_PUSH and m.payload.decode(errors="ignore") == "ON":
            print(f"[audio] {cam} 检测到 {label}", flush=True)
            threading.Thread(target=notify_audio, args=(cam, label), daemon=True).start()
        return  # dBFS、rms 等音量数据直接忽略
    try:
        p = json.loads(m.payload)
        t, before, after = p.get("type"), p.get("before", {}), p.get("after", {})
        if after.get("severity") != "alert":
            return
        if t == "new" or (t == "update" and before.get("severity") != "alert"):
            # 在后台线程中处理，取图和 AI 调用时不阻塞 MQTT 接收
            threading.Thread(target=notify, args=(after,), daemon=True).start()
    except Exception as e:
        print(f"[error] {e}", flush=True)


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message
while True:
    try:
        client.connect(os.environ.get("MQTT_HOST", "mosquitto"), 1883, 60)
        client.loop_forever()
    except Exception as e:
        print(f"[mqtt] 连接失败，10秒后重试: {e}", flush=True)
        time.sleep(10)
