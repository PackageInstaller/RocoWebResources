#!/usr/bin/env python3
"""
洛克王国资源下载
================
ver.config / Angel.config 在内存中处理，不落盘。
所有下载内容统一在 Resources/ 目录下。

直接运行：python download_resources.py
"""

import os
import re
import sys
import json
import time
import queue
import logging
import threading
import zipfile
from io import StringIO, BytesIO
from collections import defaultdict
from urllib.parse import urlparse

import requests
from tqdm import tqdm

# ─── 日志 ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("download.log"), logging.StreamHandler()],
)
logger = logging.getLogger("roco")

# ─── 路径 ─────────────────────────────────────────────────
BASE_URL = "https://res.17roco.qq.com"
RES_DIR  = "Resources"                     # 所有输出统一在这里
PROGRESS = os.path.join(RES_DIR, "progress.json")
CONFIGS  = os.path.join(RES_DIR, "configs")
EXTRACT  = os.path.join(RES_DIR, "Angel")
DOWNLOAD = os.path.join(RES_DIR, "download")

WORKERS  = 30
RETRIES  = 5

# 为 SESSION 挂载大连接池，避免 "Connection pool is full"
SESSION  = requests.Session()
adapter = requests.adapters.HTTPAdapter(
    pool_connections=50, pool_maxsize=50, max_retries=0)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 6.2; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/84.0.4147.105 Safari/537.36",
    "Referer": "https://17roco.qq.com/",
})


# ═══════════════════════════════════════════════════════════
#  网络
# ═══════════════════════════════════════════════════════════

def http_get(u, timeout=30):
    for _ in range(RETRIES):
        try:
            r = SESSION.get(u, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException:
            time.sleep(2)
    return None


def download(u, local):
    """下载文件，返回 (ok, reason)。reason: ok/304/404/500/timeout/..."""
    os.makedirs(os.path.dirname(local), exist_ok=True)
    last_err = "unknown"
    for _ in range(RETRIES):
        try:
            hdr = {}
            if os.path.exists(local) and os.path.getsize(local) > 0:
                hdr["Range"] = f"bytes={os.path.getsize(local)}-"
            r = SESSION.get(u, stream=True, timeout=30, headers=hdr)
            if r.status_code == 304:
                return (True, "304")
            if r.status_code == 404 or r.status_code == 403:
                return (False, str(r.status_code))  # 不重试
            if r.status_code not in (200, 206):
                last_err = str(r.status_code)
                time.sleep(1)
                continue
            mode = "ab" if r.status_code == 206 else "wb"
            with open(local, mode) as f:
                for c in r.iter_content(8192):
                    if c:
                        f.write(c)
            return (True, "ok")
        except requests.exceptions.Timeout:
            last_err = "timeout"
            time.sleep(2)
        except requests.exceptions.ConnectionError:
            last_err = "conn"
            time.sleep(2)
        except Exception as e:
            last_err = type(e).__name__
            time.sleep(2)
    return (False, last_err)


# ═══════════════════════════════════════════════════════════
#  工具
# ═══════════════════════════════════════════════════════════

def xml_ids(content_or_path, tag, attr="id"):
    """从 XML 内容（字符串）或文件路径提取数字 ID"""
    if os.path.isfile(str(content_or_path)):
        with open(content_or_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    else:
        content = content_or_path
    ids = set()
    for val in re.findall(rf'<{tag}\s[^>]*{attr}\s*=\s*"(\d+)"', content):
        ids.add(int(val))
    return ids


def xml_attrs(content, tag, *attrs):
    """从 XML 内容提取 <tag a1="v1" a2="v2"/> """
    rows = []
    for raw in re.findall(rf'<{tag}\s([^>]+?)\s*/?>', content):
        row = {}
        for a in attrs:
            m = re.search(rf'{a}\s*=\s*"([^"]*)"', raw)
            row[a] = m.group(1) if m else ""
        rows.append(row)
    return rows


# ═══════════════════════════════════════════════════════════
#  Phase 1 — 配置（内存中处理 ver.config / Angel.config）
# ═══════════════════════════════════════════════════════════

VER_XML = ""  # 内存中的 ver.xml 内容


def download_refresh():
    """下载 ver.config 并解压，返回 ver.xml 内容"""
    global VER_XML
    logger.info("=" * 50)
    logger.info("Phase 1: 下载配置")

    # ── ver.config → 内存中解压→ ver.xml 内容 ──
    logger.info("  下载 ver.config ...")
    r = http_get(f"{BASE_URL}/ver.config")
    if not r:
        raise SystemExit("ver.config 下载失败")

    data = r.content
    # 尝试 ZIP
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            for n in zf.namelist():
                if n.endswith(".xml"):
                    VER_XML = zf.read(n).decode("utf-8", errors="ignore")
                    break
        logger.info("    ver.xml (ZIP)")
    except zipfile.BadZipFile:
        idx = data.find(b"<?xml")
        if idx >= 0:
            VER_XML = data[idx:].decode("utf-8", errors="ignore")
        logger.info("    ver.xml (raw)")

    cnt = len(re.findall(r'<file\s+path="', VER_XML))
    logger.info(f"    {cnt} 文件")

    # ── Angel.config → 内存中下载 → 内存中解析 → 导出 XML ──
    if not os.path.exists(os.path.join(EXTRACT, "04_scene_conf.xml")):
        logger.info("  下载 Angel.config ...")
        r = http_get(f"{BASE_URL}/conf/Angel.config")
        if r:
            _parse_angel_in_memory(r.content)
    else:
        logger.info("  Angel 已有")

    # ── 场景 NPC 配置 ──
    _fetch_npc_configs()


def _parse_angel_in_memory(raw):
    """在内存中解析 Angel.config 并导出 XML"""
    try:
        from AngelConfigParser import AngelConfigParser

        p = AngelConfigParser(raw)  # 直接传 bytes
        saved = sys.stdout
        sys.stdout = StringIO()
        ok = p.parse()
        sys.stdout = saved

        if ok:
            os.makedirs(EXTRACT, exist_ok=True)
            p.export_configs(EXTRACT)
            cnt = len([f for f in os.listdir(EXTRACT) if f.endswith(".xml")])
            logger.info(f"    Angel: {cnt} XML")
    except Exception as e:
        logger.warning(f"    Angel.config 解析失败: {e}")


def _fetch_npc_configs():
    """下载场景 npc.xml"""
    os.makedirs(CONFIGS, exist_ok=True)

    # 优先从 Angel/scene_conf.xml 读场景 ID
    fp = os.path.join(EXTRACT, "04_scene_conf.xml")
    scene_ids = xml_ids(fp, "SceneDes") if os.path.exists(fp) else set()

    # 降级：从 ver.xml
    if not scene_ids:
        scene_ids = xml_ids(VER_XML, "SceneDes") if VER_XML else set()
        # 还是从路径提取
        for m in re.finditer(r'res/scene/(\d+)/', VER_XML):
            scene_ids.add(int(m.group(1)))

    new = 0
    for sid in scene_ids:
        local = os.path.join(CONFIGS, f"scene_npc_{sid}.xml")
        if not os.path.exists(local):
            r = http_get(f"{BASE_URL}/res/scene/{sid}/npc.xml", timeout=10)
            if r and len(r.content) > 50:
                with open(local, "wb") as f:
                    f.write(r.content)
                new += 1
    logger.info(f"  场景NPC: {len(scene_ids)} 场景, +{new}")


# ═══════════════════════════════════════════════════════════
#  Phase 2 — 收集 ID
# ═══════════════════════════════════════════════════════════

def collect_ids():
    logger.info("=" * 50)
    logger.info("Phase 2: 收集 ID")

    data = {}

    # 精灵
    data["spirit_ids"] = set()
    for fn in ["22_SpiritBooks.xml", "19_spirit_conf.xml"]:
        data["spirit_ids"] |= xml_ids(os.path.join(EXTRACT, fn), "spirit")

    # 场景 + 音乐
    fp = os.path.join(EXTRACT, "04_scene_conf.xml")
    sc = open(fp, encoding="utf-8", errors="ignore").read() if os.path.exists(fp) else VER_XML
    data["scene_ids"]  = xml_ids(sc, "SceneDes")
    data["music_names"] = set()
    for row in xml_attrs(sc, "SceneDes", "bgMusic"):
        for name in row.get("bgMusic", "").split("|"):
            name = name.strip().replace(".swf", "")
            if name:
                data["music_names"].add(name)

    # NPC
    data["npc_ids"] = set()
    if os.path.isdir(CONFIGS):
        for fname in os.listdir(CONFIGS):
            if fname.startswith("scene_npc_"):
                data["npc_ids"] |= xml_ids(os.path.join(CONFIGS, fname), "Npc", "npcID")
    for m in re.finditer(r'res/npc/(\d+)/', VER_XML):
        data["npc_ids"].add(int(m.group(1)))

    # 插件名
    data["plugin_names"] = set()
    fp = os.path.join(EXTRACT, "05_plugins_conf.xml")
    if os.path.exists(fp):
        with open(fp, encoding="utf-8", errors="ignore") as f:
            for m in re.finditer(r'name="(\w+)"', f.read()):
                n = m.group(1)
                if n != "plugins_conf":
                    data["plugin_names"].add(n)

    # 活动 / 道具 / 装扮 / 天赋 / 称号 — 来自 ver.xml
    for key, pattern in [
        ("activity_ids", r'activity/(\d+)/'),
        ("item_ids",    r'res/item/(\d+)\.png'),
        ("talent_ids",  r'res/talent/(\d+)_'),
        ("title_ids",   r'res/titile/(\d+)\.swf'),
    ]:
        data[key] = {int(m.group(1)) for m in re.finditer(pattern, VER_XML)}

    data["dress_ids"] = {int(m.group(1)) for m in
                          re.finditer(r'res/avatar_new/\d+/(\d+)\.png', VER_XML)}

    for k, v in data.items():
        logger.info(f"  {k:16s}: {len(v):>6}")

    return data


# ═══════════════════════════════════════════════════════════
#  Phase 3 — 生成 URL
# ═══════════════════════════════════════════════════════════

def generate_urls(data):
    logger.info("=" * 50)
    logger.info("Phase 3: 生成 URL")

    U = defaultdict(list)

    # 场景
    # 场景 — 只取 ver.xml 中确认存在的 scene.swf / npc.xml
    scene_swf_ids = {int(m.group(1)) for m in
                     re.finditer(r'res/scene/(\d+)/scene\.swf', VER_XML)}
    scene_npc_ids = {int(m.group(1)) for m in
                     re.finditer(r'res/scene/(\d+)/npc\.xml', VER_XML)}
    for sid in scene_swf_ids:
        U["scene"].append(f"{BASE_URL}/res/scene/{sid}/scene.swf")
    for sid in scene_npc_ids:
        U["scene"].append(f"{BASE_URL}/res/scene/{sid}/npc.xml")

    # 音乐 — 只用 scene_conf.xml 中实际出现的
    for name in data["music_names"]:
        U["music"].append(f"{BASE_URL}/res/music/{name}.swf")
    # 补充战斗BGM和音效
    for name in ["CombatBGMusic_pve", "EffectSoundAll"]:
        U["music"].append(f"{BASE_URL}/res/music/{name}.swf")

    # 精灵
    for sid in data["spirit_ids"]:
        U["spirit"].append(f"{BASE_URL}/res/spirit/{sid}-.swf")
        U["combat"] += [
            f"{BASE_URL}/res/combat/spirits/{sid}-.swf",
            f"{BASE_URL}/res/combat/previews/{sid}-idle.swf",
            f"{BASE_URL}/res/combat/icons/{sid}-.png",
        ]

    # NPC — swf 是必须的，preview 不一定存在
    for nid in data["npc_ids"]:
        U["npc"].append(f"{BASE_URL}/res/npc/{nid}/{nid}.swf")

    # 道具
    for iid in data["item_ids"]:
        U["item"].append(f"{BASE_URL}/res/item/{iid}.png")

    # 装扮
    for did in data["dress_ids"]:
        for sub in [12, 13, 14, 16, 25, 3, 6, 7, 8, 1, 20]:
            U["dress"].append(f"{BASE_URL}/res/avatar_new/{sub}/{did}.png")

    # 活动 — ui 和 s 从 ver.xml 实际存在来，config.xml 不一定有
    for aid in data["activity_ids"]:
        U["activity"].append(f"{BASE_URL}/activity/{aid}/ui{aid}.swf")
        U["activity"].append(f"{BASE_URL}/activity/{aid}/s{aid}.swf")
    # config.xml 只取 ver.xml 中确认存在的
    cfg_ids = {int(m.group(1)) for m in
               re.finditer(r'activity/(\d+)/config\.xml', VER_XML)}
    for aid in cfg_ids:
        U["activity"].append(f"{BASE_URL}/activity/{aid}/config.xml")

    # 插件
    for pname in data["plugin_names"]:
        U["plugins"].append(f"{BASE_URL}/plugins/{pname}/{pname}PluginLib.swf")

    # 天赋 — small.png 从 ver.xml 验证，des.swf 也验证
    talent_small = {int(m.group(1)) for m in
                    re.finditer(r'res/talent/(\d+)_small\.png', VER_XML)}
    talent_des = {int(m.group(1)) for m in
                  re.finditer(r'res/talent/(\d+)_des\.swf', VER_XML)}
    for tid in talent_small:
        U["talent"].append(f"{BASE_URL}/res/talent/{tid}_small.png")
    for tid in talent_des:
        U["talent"].append(f"{BASE_URL}/res/talent/{tid}_des.swf")

    # 称号
    for tid in data["title_ids"]:
        U["title"].append(f"{BASE_URL}/res/titile/{tid}.swf")

    # 战斗特效 — 只取 ver.xml 中实际存在的
    effect_ids = {int(m.group(1)) for m in
                  re.finditer(r'res/combat/effects/(\d+)\.swf', VER_XML)}
    for eid in effect_ids:
        U["combat"].append(f"{BASE_URL}/res/combat/effects/{eid}.swf")

    # 世界地图 — 只取 ver.xml 中实际记录的 map/begin ID
    world_map_ids = {int(m.group(1)) for m in
                     re.finditer(r'plugins/WorldMap/maps/(\d+)\.swf', VER_XML)}
    world_begin_ids = {int(m.group(1)) for m in
                       re.finditer(r'plugins/WorldMap/begin/(\d+)\.swf', VER_XML)}
    for mid in world_map_ids:
        U["worldmap"].append(f"{BASE_URL}/plugins/WorldMap/maps/{mid}.swf")
    for mid in world_begin_ids:
        U["worldmap"].append(f"{BASE_URL}/plugins/WorldMap/begin/{mid}.swf")

    # 全局
    U["swf"] += [f"{BASE_URL}/swf/ROCO-Z8.swf", f"{BASE_URL}/swf/bb.swf"]
    U["config"] += [
        f"{BASE_URL}/ver.config", f"{BASE_URL}/Global.xml",
        f"{BASE_URL}/conf/Angel.config",
        f"{BASE_URL}/plugins/Home/conf/FurnitureConstInfo.config",
        f"{BASE_URL}/plugins/Manor/ManorNpc.xml",
        f"{BASE_URL}/plugins/Manor/Beautify/BeautifyConf.xml",
    ]

    # ver.xml 完整清单
    for m in re.finditer(r'<file\s+path="([^"]+)"', VER_XML):
        U["verxml"].append(f"{BASE_URL}/{m.group(1)}")

    # 去重
    total = 0
    for cat in U:
        U[cat] = sorted(set(U[cat]))
        n = len(U[cat])
        total += n
        logger.info(f"  {cat:12s}: {n:>6}")
    logger.info(f"  总计: {total}")
    return U


# ═══════════════════════════════════════════════════════════
#  Phase 4 — 下载
# ═══════════════════════════════════════════════════════════

def get_local(u):
    p = urlparse(u).path.lstrip("/")
    h = urlparse(u).hostname or ""
    if "tencent-cloud" in h:
        return os.path.join(RES_DIR, "cdn", "tencent_cloud", p)
    return os.path.join(DOWNLOAD, p)


class Downloader:
    def __init__(self):
        self.done = set()
        self.fail = {}
        self.lock = threading.Lock()
        if os.path.exists(PROGRESS):
            try:
                d = json.load(open(PROGRESS))
                self.done = set(d.get("completed", []))
                self.fail = d.get("failed", {})
            except Exception:
                pass

    def save(self):
        with self.lock:
            os.makedirs(RES_DIR, exist_ok=True)
            json.dump({"completed": sorted(self.done),
                        "failed": self.fail},
                       open(PROGRESS, "w"), indent=2)

    def run(self, all_urls):
        if isinstance(all_urls, dict):
            flat = []
            for us in all_urls.values():
                flat.extend(us)
        else:
            flat = all_urls

        flat = list(dict.fromkeys(flat))
        # 待下载 = 未完成 + 上次失败的（给重试机会）
        pending = [u for u in flat if u not in self.done or u in self.fail]
        for u in pending:
            self.fail.pop(u, None)

        if not pending:
            logger.info("全部下载完成")
            return

        logger.info(f"待下载: {len(pending)} / {len(flat)}")

        q = queue.Queue()
        for u in pending:
            q.put(u)
        stats = {"ok": 0, "skip": 0, "fail": 0}

        def worker():
            while True:
                try:
                    u = q.get(block=False)
                except queue.Empty:
                    break
                try:
                    local = get_local(u)
                    if os.path.exists(local) and os.path.getsize(local) > 0:
                        with self.lock: self.done.add(u)
                        stats["skip"] += 1
                        continue
                    ok, reason = download(u, local)
                    if ok:
                        with self.lock: self.done.add(u)
                        stats["ok"] += 1
                    else:
                        with self.lock: self.fail[u] = reason
                        stats["fail"] += 1
                        if reason == "404":
                            logger.debug(f"  404 {u}")
                except Exception as e:
                    with self.lock: self.fail[u] = type(e).__name__
                    stats["fail"] += 1
                finally:
                    q.task_done()

        n = min(WORKERS, len(pending))
        ts = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
        for t in ts:
            t.start()

        pbar = tqdm(total=len(pending), desc="下载", unit="file")
        last = 0
        while any(t.is_alive() for t in ts):
            cur = len(self.done)
            if cur > last:
                pbar.update(cur - last)
                last = cur
            time.sleep(0.5)
        pbar.update(len(self.done) - last)
        pbar.close()
        for t in ts:
            t.join(timeout=5)

        self.save()
        logger.info(f"完成: +{stats['ok']} 跳过{stats['skip']} 失败{stats['fail']}")
        if self.fail:
            from collections import Counter
            rc = Counter(self.fail.values())
            logger.info("  失败原因: " + ", ".join(f"{k}x{v}" for k, v in rc.most_common(6)))
            # 按 URL 路径前缀分组，每类展示 3 个样例
            prefix_groups = defaultdict(list)
            for u in self.fail:
                p = urlparse(u).path
                parts = p.lstrip("/").split("/")
                prefix = "/".join(parts[:3]) if len(parts) >= 3 else "/".join(parts[:2])
                prefix_groups[prefix].append(u)
            logger.info("  失败 URL 分布:")
            for prefix, urls in sorted(prefix_groups.items(), key=lambda x: -len(x[1])):
                samples = urls[:3]
                logger.info(f"    {prefix:40s} x{len(urls):<5}  e.g. {samples[0].split('/')[-1]}")


# ═══════════════════════════════════════════════════════════

def main():
    start = time.time()
    download_refresh()
    data = collect_ids()
    urls = generate_urls(data)
    Downloader().run(urls)
    logger.info(f"总耗时 {time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
