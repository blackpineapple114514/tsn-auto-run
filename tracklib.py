# -*- coding: utf-8 -*-
"""tracklib.py —— 校园跑轨迹内核（自研实现；架构参考公开项目 TiShiNengRunning，代码为本仓原创）。

职责：把「地图上画的一条折线」变成「一份能通过客户端自校验、且与 17-接口规格书
完全对齐的跑步记录」——轨迹点 / 60s 步数桶 / 人脸点 / 合格性 / 21 参上传表单。

坐标约定
--------
* 本模块内部一律用 **(lng, lat)**（GeoJSON 序）。
* 对外 dict 输出用 app 的键名（`latitude`/`longitude` 或单字母 `a`/`o`）。
* 底图是 OSM ⇒ **显示坐标 = WGS-84**；app（高德定位）用 **GCJ-02** ⇒
  导出时用 `wgs84_to_gcj02()` 偏移（默认开，可用 `crs='wgs84'` 关）。

自洽铁律（与 tools/mock_client_sim.py 同一判据）
-----------------------------------------------
每一步位移 `distance = speed × dt`，累计距离 = 逐点求和 ⇒
`speed` / `formatSportRange` / `avgspeed` / 配速 四者必须互相推得出来。
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

import device_profile as DP      # 设备 / app 身份的唯一出处（默认 = 真机 iPhone 基线）

EARTH_R = 6371008.8
M_PER_DEG_LAT = 111320.0

# 步幅模型（m）：随速度线性增长并夹逼 —— 与参考实现同形，**系数按真机合格记录定标**。
# 定标：记录 示例记录（3110 m / 2401 步 ⇒ **聚合步幅 1.295 m**，运动均速 2.674 m/s）。
# 注意要对的是**聚合**（Σ里程/Σ步数）而不是「速度取均值后的点估」——快档样本拿长步幅、
# 而且它们背了不成比例的距离。本轮扫参（base/slope → 步数 / 聚合步幅，moving 口径改造后）：
#   0.700/0.200 → 2413 / 1.289 ; 0.710/0.192 → 2436 / 1.277 ; 0.720/0.195 → 2375 / 1.310
#   0.705/0.195 → **2402 / 1.2948** ⇒ 取 0.705 / 0.195：步数 2402（真机 2401）、
#   派生步幅 129 cm（真机 129）、步频 124.6（真机 123.9）、均速 9.68（真机 9.63）。
# 旧值 0.6/0.14 在 v=2.67 只有 0.97 m —— 同样里程要多跑 34% 的步数。
STRIDE_BASE_M = 0.705
STRIDE_SLOPE_S = 0.195
STRIDE_MIN_M = 0.55
STRIDE_MAX_M = 1.55
# 性别系数：同一配速下女性步幅约为男性的 94%（身高 / 腿长差异的经验修正）
STRIDE_SEX_FACTOR = {"male": 1.0, "female": 0.94}

BUCKET_SECONDS = 60.0
FACE_RATIO_MIN, FACE_RATIO_MAX = 0.05, 1.2
FACE_MIN_GAP_S = 5.0

# 「跑道连续跑」模式（loop_mode="track"）：整条轨迹一口气跑完，**圈与圈自然衔接**，
# 每圈只换「横向位置」与「快慢」——起点/方向是整条一次定的，不逐圈重开（否则会瞬移，反而假）。
TRACK_LATERAL_M = 0.4            # 每圈横向（道次内外侧）漂移幅度 ±，逐圈缓慢随机游走
TRACK_PACE_PCT = 0.03            # 每圈配速扰动 ±3%
TRACK_COORD_JITTER_DEG = 0.000015  # 点级 GPS 量化抖动建议值 ±（≈1.7 m，真人手机定位的量级）
TRACK_LAP_CACHE = 4096           # 「圈配速」**预抽**的条数（见 resample_path 的说明）

# ---- PointData 固定取值（对齐真机合格记录 2026-09-24 逐字段核对）----
# 真机 9 键 a,c,e,i,l,m,o,s,t 值**全是字符串**：坐标 6 位、速度 6 位小数；
# l = 定位类型（常态 "2"，暂停段 "3"），m 恒 "0"。
POINT_LOCATION_TYPE = "2"
POINT_LOCATION_TYPE_PAUSED = "3"
POINT_M = "0"

# ---- 起点 / 停顿段形态（对齐真机合格记录 示例记录，2026-09-27 B0→B1/B2）----
# 真机事实（逐点核过）：
#   * 首个导出点 `c`="5"、`e`="0" ⇒ 计时先跑了 ~4 s 才落点（前 4 s 的采样没进数组）；
#   * 中途停顿段是**成组**的：3 点 l="3"（Δt = 59722/137/3188 ms，c/e 冻结、i=true）
#     → 4 点 l="2" 仍冻结（i=true，位置大跳 ⇒ s 4.5~12.5，即 GPS 重捕获）
#     → 9 点 l="1"（i=false，c 恢复 +1/s，慢跑 1.4~2.8 m/s）→ 回 l="2" 常态。
# 旧实现停顿只落 **1 个** l="3" 点、且没有 l="1" —— 形态与真机差得肉眼可见。
START_SKIP_S = 4.0                 # 起步前 N 秒计时已走但不落点 ⇒ 首点 `c` = N+1
PAUSE_FROZEN_N = 3                 # 停顿段 l="3" 的点数（真机 3）
PAUSE_TINY_DT_S = (0.137, 3.188)    # 另两个冻结点的小 Δt（真机 137 ms / 3188 ms）
POST_PAUSE_FROZEN_N = 4            # 停顿后仍冻结（i=true）的点数（真机 4）
POST_PAUSE_L1_N = 9                # 停顿后恢复段点数（l="1"、i=false；真机 9）
POST_PAUSE_JUMP_MPS = (4.5, 12.5)   # 冻结点速度带（GPS 重捕获跳变）
POST_PAUSE_JUMP_SEQ = (9.8, 5.1, 4.9, 11.6)   # 逐点固定取值（真机 10.09/4.97/4.74/12.37）
                                   # ⚠️ 固定值、**不抽 rng**：抽随机会让「配速定标的干跑」与
                                   #    正式跑在不同 scale 下抽到不同跳距 ⇒ t(scale) 失去单调性，
                                   #    二分定标实测偏 8 s（plan 1163 → 落在 1155~1168）。
PAUSE_RECOVER_SCALE = 0.7          # 恢复段速度倍率（刚停完慢慢起速）
POINT_LOCATION_TYPE_RECOVER = "1"   # 恢复段定位类型（真机 9 个点）

# ---- 采样「传感器节奏」（真机 dt 分布：1000×1105 / 2000×20 / 0×16 / 999,1001 各 7）----
SAMPLE_DROP_P = 0.02       # 丢帧概率：这一秒没拿到 GPS fix ⇒ t 间隔翻倍（不落点，但继续前进）
CLOCK_JITTER_P = 0.012     # ±1 ms 时钟抖动概率（真机 dt 里 999/1001 的来源）
STEP_BURST_THRESHOLD = 5.0  # 步数传感器「成批上报」阈值：累计到这么多才刷新一次 e
DUP_P = 0.014              # 重复点概率（真机 dt==0 占 1.4%：同 t / 同坐标 / s=0）

# ---- 真实感：配速档（真机点速度 CV≈0.50，速度分段：停 2.6% / 走 21.5% / 跑 70% / 尖峰 0.9%）----
# 匀速一条线是「算出来的」最明显的特征；真人一段快一段慢，还夹着走。
PACE_SEG_S = (15.0, 60.0)   # 一个「配速档」持续的秒数
PACE_RUN = (0.85, 1.40)     # 跑档：相对均速的倍率
PACE_WALK = (0.20, 0.58)    # 走档（真机 21.5% 的点落在 0.4~1.6 m/s）
PACE_WALK_P = 0.26          # 走档出现概率（真机 21.5% 的点落在 0.4~1.6 m/s）
PACE_STOP = (0.03, 0.12)    # 停档（等红灯 / 系鞋带；真机 2.6% 的点 < 0.4 m/s）
PACE_STOP_P = 0.04          # 停档出现概率（真机 2.6% 的点 < 0.4 m/s）
PACE_MICRO = 0.05           # 档内逐点微抖动（σ）

# ---- 真实感：手机定位噪声（三项都拿真机合格记录实测校准，见 skills/run-trace-realism）----
# 真机（本人账号读回 示例记录，1161 点）实测：
#   * 点级「局部直线残差」（P_i 到 P_{i-1}P_{i+1} 的垂距）中位 0.153 m / p90 0.498 m；
#   * 相邻点位移 ÷ 上报速度 `s` 的比值：中位 1.000、p10 0.974、p90 1.025（±2.5%）；
#   * 点串折线长 +3.3%（3.213 km vs 记录 sportRange 3.110 km）。
# ⇒ 噪声**必须空间相关**：纯点级白噪会把「位移/速度」比值 p90 推到 1.128（13%）、
#    折线长灌到 +6.7% —— 两条都是「速度跟坐标对不上」的现成指纹。
# 于是拆成：低频漂移（占大头，慢游走 ⇒ 几乎不改长度、不扰相邻位移）+ 少量点级白噪
# （定位量化级）+ 偶发多径野值。
GPS_NOISE_M = 0.22          # 噪声总强度（米）—— UI / 接口沿用这一个旋钮
GPS_DRIFT_STEP = 1.55       # 横向慢游走每步增量 σ（× 总强度）
GPS_DRIFT_KEEP = 0.97       # 横向慢游走保持率（相关时间 ≈ 33 点 ⇒ 更慢更平滑，抬「到中心线垂距」而不抬高频残差）
GPS_PERP_FRAC = 0.46        # 横向白噪 σ 占比 → 点级残差量级
GPS_ALONG_FRAC = 0.10       # 纵向（沿运动方向）白噪 σ 占比 → 折线长度膨胀量级
GPS_OUTLIER_P = 0.008       # 多径野值概率（真机位移 > 6 m 的点占 1.12%）
GPS_OUTLIER_M = 6.0         # 野值附加量级上限（米；真机位移 p99.9 = 12.3 m、常态 2.93 m）
GPS_OUTAGE_P = 0.5          # 每次跑步触发一次「GPS 断记」的概率（0=关；不是每次都有）
GPS_OUTAGE_MIN_S = 15.0     # 断记时长下限（秒）
GPS_OUTAGE_MAX_S = 60.0     # 断记时长上限（秒，也是窗口硬上限）
GPS_OUTAGE_MIN_M = 20.0     # 跨空档位移下限（米）
GPS_OUTAGE_MAX_M = 75.0     # 跨空档位移上限（米）；真机那条 = 54 m


class TrackError(ValueError):
    pass


# --------------------------------------------------------------------------- 几何
def m_per_deg_lng(lat: float) -> float:
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def normalize_path(raw: Sequence[Any]) -> List[List[float]]:
    """接受 [{lat,lng} | {latitude,longitude}] 或 [[lng,lat] | [lat,lng]...]。
    list 形式按 **GeoJSON [lng,lat]** 解释（纬度必在 ±90，越界则自动纠正）。"""
    out: List[List[float]] = []
    for item in raw or []:
        if isinstance(item, dict):
            lat = item.get("lat", item.get("latitude", item.get("a")))
            lng = item.get("lng", item.get("lon", item.get("longitude", item.get("o"))))
            if lat is None or lng is None:
                continue
            lat, lng = float(lat), float(lng)
        else:
            if len(item) < 2:
                continue
            x, y = float(item[0]), float(item[1])
            if abs(x) <= 90 and abs(y) > 90:  # 传成了 [lat,lng]
                lat, lng = x, y
            else:
                lng, lat = x, y
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        out.append([lng, lat])
    dedup: List[List[float]] = []
    for p in out:
        if not dedup or haversine(dedup[-1][1], dedup[-1][0], p[1], p[0]) > 0.05:
            dedup.append(p)
    return dedup


def path_length_m(path: Sequence[Sequence[float]]) -> float:
    return sum(haversine(path[i - 1][1], path[i - 1][0], path[i][1], path[i][0])
               for i in range(1, len(path)))


# --------------------------------------------------------------------------- 走线器
class _Walker:
    """沿折线走的「走线器」。

    ``loop_mode``：``repeat`` / ``track`` —— 走到头回起点接着走（封闭环线「绕圈」）；
    ``track`` 额外带**每圈横向漂移**（模拟沿跑道内外侧偏，逐圈缓慢游走，圈间不重新起跑）；
    ``pingpong`` —— 折返；``once`` —— 只走一遍，走完报错。
    """

    def __init__(self, points: Sequence[Sequence[float]], loop_mode: str,
                 rng: Optional[random.Random] = None, lateral_m: float = 0.0):
        self.base = [list(p) for p in points]
        if loop_mode in ("repeat", "track"):
            if haversine(self.base[-1][1], self.base[-1][0],
                         self.base[0][1], self.base[0][0]) > 1e-6:
                self.base.append(list(self.base[0]))
        self.loop_mode = loop_mode
        self.rng = rng or random.Random()
        self.lateral_m = max(0.0, float(lateral_m))
        self.lap = 0
        self.lateral_prev = 0.0     # 本圈起点的横向偏移
        self.lateral = 0.0          # 本圈终点的横向偏移（圈内线性过渡 ⇒ 全程连续，无「瞬移」）
        self._drift()
        self._reset(self.base)

    def _drift(self) -> None:
        """翻到下一圈：横向偏移在上一次的基础上缓慢随机游走，夹在 ±lateral_m 内。

        ``lateral_prev`` 记本圈起点值、``lateral`` 记本圈终点值；圈内由 ``point()`` 线性插值，
        所以跨圈处两侧取值相等 ⇒ 位置连续（真人沿跑道缓慢内外漂，而不是每圈瞬移）。
        """
        self.lateral_prev = self.lateral
        if self.lateral_m <= 0:
            self.lateral = 0.0
            return
        step = self.rng.uniform(-0.6, 0.6) * self.lateral_m
        self.lateral = max(-self.lateral_m, min(self.lateral_m, self.lateral + step))

    def _reset(self, pts: List[List[float]]) -> None:
        self.points = [list(p) for p in pts]
        self.segs = [haversine(self.points[i][1], self.points[i][0],
                               self.points[i + 1][1], self.points[i + 1][0])
                     for i in range(len(self.points) - 1)]
        self._cum = [0.0]
        for s in self.segs:
            self._cum.append(self._cum[-1] + s)
        self.total = self._cum[-1]
        self.i = 0
        self.off = 0.0

    def point(self) -> List[float]:
        if self.i >= len(self.segs):
            return list(self.points[-1])
        a, b = self.points[self.i], self.points[self.i + 1]
        L = self.segs[self.i]
        if L <= 0:
            return list(a)
        r = min(1.0, max(0.0, self.off / L))
        lng = a[0] + (b[0] - a[0]) * r
        lat = a[1] + (b[1] - a[1]) * r
        if self.lateral == 0.0 and self.lateral_prev == 0.0:
            return [lng, lat]
        # 圈内线性过渡：起点 lateral_prev → 终点 lateral（跨圈两侧取值相等，位置连续）
        frac = 0.0
        if self.total > 0:
            frac = min(1.0, max(0.0, (self._cum[min(self.i, len(self._cum) - 1)] + self.off) / self.total))
        cur = self.lateral_prev + (self.lateral - self.lateral_prev) * frac
        if cur == 0.0:
            return [lng, lat]
        # 横向偏移：在当前线段**法向**上偏 lateral 米（只改左右，不改前后进度）
        cosl = m_per_deg_lng((a[1] + b[1]) / 2.0)
        dx = (b[0] - a[0]) * cosl
        dy = (b[1] - a[1]) * M_PER_DEG_LAT
        seg = math.hypot(dx, dy)
        if seg <= 0:
            return [lng, lat]
        px, py = -dy / seg, dx / seg
        return [lng + px * cur / cosl, lat + py * cur / M_PER_DEG_LAT]

    def advance(self, dist: float) -> None:
        rem = dist
        while rem > 1e-9:
            if self.i >= len(self.segs):
                self._wrap()
                continue
            L = self.segs[self.i]
            if L <= 0:
                self.i += 1
                self.off = 0.0
                continue
            avail = L - self.off
            if avail > rem:
                self.off += rem
                rem = 0.0
            else:
                rem -= avail
                self.i += 1
                self.off = 0.0

    def _wrap(self) -> None:
        if self.loop_mode == "pingpong":
            self._reset(list(reversed(self.points)))
        elif self.loop_mode in ("repeat", "track"):
            self._reset(self.base)
            if self.loop_mode == "track":
                self.lap += 1
                self._drift()      # 翻到下一圈：换一个横向漂移终点；起点不重置 ⇒ 连贯跑，无瞬移
        else:
            raise TrackError("路径已走完（loop_mode=once）；请加大路径或改用 repeat/pingpong/track")


# --------------------------------------------------------------------------- 步幅 / 步数
def stride_for_speed(speed_mps: float, sex: str = "male") -> float:
    """步幅（m）：随速度线性增长并夹逼；再乘性别系数（男 1.00 / 女 0.94）。"""
    v = max(0.0, float(speed_mps))
    f = STRIDE_SEX_FACTOR.get(str(sex or "male").lower(), 1.0)
    return min(STRIDE_MAX_M, max(STRIDE_MIN_M, (STRIDE_BASE_M + STRIDE_SLOPE_S * v) * f))


def steps_for(distance_m: float, speed_mps: float, sex: str = "male",
              stride_m: Optional[float] = None, stride_jitter: float = 0.0,
              rng: Optional[random.Random] = None) -> float:
    """步数 = 距离 / 步幅。

    ``stride_m`` 给了就用它（显式覆盖）；``stride_jitter`` > 0 时按 ±j 随机抖动
    （「随机模式」：真人同配速下的步幅也不是定值，不该均匀取同一个数）。
    """
    s = float(stride_m) if (stride_m and float(stride_m) > 0) else stride_for_speed(speed_mps, sex)
    if stride_jitter > 0 and rng is not None:
        s *= 1.0 + rng.uniform(-stride_jitter, stride_jitter)
    return 0.0 if s <= 0 else max(0.0, distance_m) / s


def _largest_remainder(values: Sequence[float], total: int) -> List[int]:
    floors = [int(v) for v in values]
    rem = total - sum(floors)
    if rem == 0:
        return floors
    if rem > 0:
        order = sorted(range(len(values)), key=lambda i: values[i] - floors[i], reverse=True)
        for i in order[:rem]:
            floors[i] += 1
        return floors
    order = sorted(range(len(values)), key=lambda i: values[i] - floors[i])
    need = -rem
    for i in order:
        if need == 0:
            break
        if floors[i] > 0:
            floors[i] -= 1
            need -= 1
    return floors


# --------------------------------------------------------------------------- 重采样
def resample_path(path: Sequence[Sequence[float]],
                  target_m: float, start_ms: int, plan_s: float,
                  sample_interval_s: float = 1.0, speed_jitter: float = 0.08,
                  interval_jitter_s: float = 0.0, loop_mode: str = "repeat",
                  pause_tail_points: int = 0,
                  sample_drop_p: float = SAMPLE_DROP_P,
                  clock_jitter_p: float = CLOCK_JITTER_P,
                  warmup_s: float = 0.0, cooldown_s: float = 0.0,
                  stop_tail_s: float = 0.0, stop_walk_mps: float = 1.5,
                  mid_pauses: Optional[Sequence[Dict[str, Any]]] = None,
                  track_lateral_m: float = TRACK_LATERAL_M,
                  track_pace_pct: float = TRACK_PACE_PCT,
                  pace_swing: float = 0.0, dup_p: float = 0.0,
                  pace_scale: float = 1.0,
                  start_skip_s: float = START_SKIP_S, pause_block: bool = True,
                  rng: Optional[random.Random] = None,
                  max_samples: int = 200000) -> List[Dict[str, Any]]:
    """沿 path 走 target_m 米，每 sample_interval_s 采一点（速度围绕基准带抖动）。

    真实感选项：
      * ``warmup_s`` / ``cooldown_s``：起步爬升 / 收尾降速（速度乘 0.8→1.0 的斜坡）。
      * ``mid_pauses``：中途暂停，形如 ``[{"at":0.45,"seconds":20}]``（``at`` = 计划用时的比例）。
        暂停点 ``speed=0 / distance=0 / isPuase=True``，仍满足 ``distance == speed×dt``。
      * ``stop_tail_s``：**计时停止后的收尾段**（真机 33 点）：人还在走（``stop_walk_mps``），
        但 ``c`` / ``e`` 冻结、不算进 sportRange / 步数 —— 这是真机合格记录里
        ``i="true"`` 而 ``l`` 仍为 ``"2"`` 的那一段（不是暂停）。
      * ``loop_mode="track"``（跑道连续跑）：**整条连贯走**，每圈只改横向位置
        （``track_lateral_m``）与快慢（``track_pace_pct``），起点/方向整条一次定；
        最后一圈按剩余距离自然截断。圈内沿用速度抖动 / 采样抖动 / 热身收尾 / 中途暂停。
      * ``start_skip_s``：起步前这么多秒**计时已走但不落点**（真机首点 `c`="5"、`e`="0"）
        —— 导出时把 moving_s 小于它的采样切掉，所以 `c` 从 N+1 起。
      * ``pause_block``：停顿段按真机形态落**成组**的点（3×l="3" → 4×l="2"仍冻结 → 9×l="1"）。
    """
    if target_m <= 0 or plan_s <= 0:
        raise TrackError("目标距离 / 计划用时必须为正")
    if sample_interval_s <= 0:
        raise TrackError("采样间隔必须为正")
    if loop_mode not in ("repeat", "pingpong", "once", "track"):
        raise TrackError("loop_mode ∈ {repeat,pingpong,once,track}")
    rng = rng or random.Random()
    base_speed = target_m / plan_s
    _target = target_m
    _jump_i = 0
    walker = _Walker(path, loop_mode, rng=rng,
                     lateral_m=(track_lateral_m if loop_mode == "track" else 0.0))
    lap_speed = 1.0
    cur_lap = 0
    # 「圈配速」**预先抽好**一整条序列：若在循环里「进圈才抽」，抽签位置会随丢帧/scale
    # 漂移 ⇒ 后面整条 rng 流错位 ⇒ t(scale) 从光滑曲线变成 ±10 s 的锯齿，
    # generate() 的二分定标就定不准（实测 elapsed 在 1155~1168 之间乱跳）。
    _lap_speeds: List[float] = []
    if loop_mode == "track" and track_pace_pct > 0:
        _lap_speeds = [1.0 + rng.uniform(-track_pace_pct, track_pace_pct)
                       for _ in range(TRACK_LAP_CACHE)]
        lap_speed = _lap_speeds[0]

    pauses: List[Tuple[float, float]] = []
    for p in (mid_pauses or []):
        if not isinstance(p, dict):
            continue
        try:
            at = float(p.get("at", 0.0))
            sec = float(p.get("seconds", 0.0))
        except (TypeError, ValueError):
            continue
        if sec <= 0:
            continue
        pauses.append((max(0.0, min(0.95, at)) * plan_s, sec))
    pauses.sort()
    pidx = 0
    _after = 0                 # 停顿后还剩几个「仍冻结 / 恢复段」的点（见 POST_PAUSE_*）

    # 真实感：配速档序列（跑 / 走）—— 先排好整段，再归一到均值 1（保证总用时 ≈ 计划）。
    segs: List[Tuple[float, float, float]] = []
    if pace_swing > 0:
        # ⚠️ 档位序列必须**只覆盖实际会用到的那段**（计划用时 + 暂停 + 一个档的余量）。
        # 以前排到 `plan_s * 1.8 + 120`，于是全程只用掉前一半档位：走档（21.5%）经常被切掉，
        # 实测速度 CV 只有 0.318（真机 0.497）、走档占比 7.1%（真机 21.5%）。
        _span = plan_s + sum(_sec for _, _sec in pauses) + 2.0 * PACE_SEG_S[1] + 60.0
        _t = 0.0
        while _t < _span:
            _r = rng.random()
            if _r < PACE_STOP_P:
                _m = rng.uniform(*PACE_STOP)
            elif _r < PACE_STOP_P + PACE_WALK_P:
                _m = rng.uniform(*PACE_WALK)
            else:
                _m = rng.uniform(*PACE_RUN)
            _d = rng.uniform(*PACE_SEG_S)
            # ⚠️ 必须**下夹逼到正数**：停档 `_m` 最小 0.03，pace_swing>1 时
            # 1+(0.03-1)*2 = -0.94，速度变负 ⇒ 采样循环抛「采样速度非正」。
            _mm = 1.0 + (_m - 1.0) * min(2.0, float(pace_swing))
            segs.append((_t, _t + _d, max(0.02, _mm)))
            _t += _d
        # 先归一到「时长加权的算术均值 = 1」（让 pace_swing 的档位幅度可比），
        # 真正的「总用时 = 计划用时」由**调用方 generate() 的干跑定标**完成：
        # 距离固定时总用时 ∝ 1/倍率，而单靠段内均值归一到不了位（走/停档把时长拉长，
        # 实测 +8%）⇒ 见 generate() 里的 `_ps`。
        _dur = sum(b - a for a, b, m in segs) or 1.0
        _mean = sum((b - a) * m for a, b, m in segs) / _dur
        _scale = (1.0 / _mean) if _mean > 0 else 1.0
        segs = [(a, b, m * _scale) for a, b, m in segs]
    # ``pace_scale``：把整条倍率统一乘一个标量（由 generate() 的干跑定标给出）。
    # 模式不变、只是整体快/慢 ⇒ 距离固定时总用时 ∝ 1/pace_scale，正是定标要的杠杆。
    if pace_scale and float(pace_scale) != 1.0 and segs:
        _k = float(pace_scale)
        segs = [(a, b, max(0.02, m * _k)) for a, b, m in segs]

    def _seg_mult(off: float) -> float:
        for a, b, m in segs:
            if a <= off < b:
                return m
        return segs[-1][2] if segs else 1.0

    samples: List[Dict[str, Any]] = []
    traveled = 0.0
    offset = 0.0
    pend_dist = 0.0          # 丢帧攒下的路程（并入下一个落点，保证总距离不减）
    pend_dt = 0.0
    while traveled < _target - 1e-9:
        if len(samples) >= max_samples:
            raise TrackError("采样点超上限；请加大采样间隔或距离/时间参数")
        # 到点了就先插停顿（真机形态：3 个 l="3" 冻结点，Δt 之和 == sec）
        if pidx < len(pauses) and offset >= pauses[pidx][0]:
            sec = pauses[pidx][1]
            pidx += 1
            _tail_trim = sum(PAUSE_TINY_DT_S)
            if pause_block and PAUSE_FROZEN_N > 1 and sec > _tail_trim + 0.5:
                _dts = [sec - _tail_trim] + list(PAUSE_TINY_DT_S)
            else:
                _dts = [sec]                       # 停顿太短就退回单点
            for _dt in _dts:
                offset += _dt
                samples.append({
                    "lng": walker.point()[0], "lat": walker.point()[1],
                    "t_ms": start_ms + offset * 1000.0, "dt_ms": _dt * 1000.0,
                    "offset_s": offset, "distance_m": 0.0,
                    "cumulative_m": traveled, "speed_mps": 0.0, "paused": True,
                })
            _after = (POST_PAUSE_FROZEN_N + POST_PAUSE_L1_N) if pause_block else 0
            continue
        if loop_mode == "track" and track_pace_pct > 0 and walker.lap != cur_lap:
            cur_lap = walker.lap                       # 进新一圈：换一次配速
            lap_speed = _lap_speeds[cur_lap % len(_lap_speeds)]
        speed = base_speed * lap_speed
        if segs:
            speed *= _seg_mult(offset)
            if PACE_MICRO > 0:
                speed *= max(0.7, min(1.3, rng.gauss(1.0, PACE_MICRO)))
        if speed_jitter > 0:
            speed = speed * min(1 + 3 * speed_jitter,
                                max(max(0.5, 1 - 3 * speed_jitter), rng.gauss(1.0, speed_jitter)))
        if warmup_s > 0 and offset < warmup_s:
            speed *= 0.8 + 0.2 * (offset / warmup_s)
        if cooldown_s > 0 and offset > plan_s - cooldown_s:
            speed *= 0.8 + 0.2 * max(0.0, (plan_s - offset) / cooldown_s)
        if speed <= 0:
            raise TrackError("采样速度非正")
        dt = sample_interval_s
        if interval_jitter_s > 0:
            dt = max(0.05, dt + rng.uniform(-interval_jitter_s, interval_jitter_s))
        nominal = speed * dt
        rem = _target - traveled
        if nominal >= rem:
            dist = rem
            dt = dist / speed
        else:
            dist = nominal
        # 停顿后的两个子段（真机形态）：
        #   * 仍冻结（i=true、不计时不计步）：GPS 一次重捕获 ⇒ 位置大跳、`s` 拉到 4.5~12.5；
        #   * 恢复段（l="1"）：刚停完慢慢起速 ⇒ 速度乘一个 <1 的系数。
        _frozen = False
        _l1 = False
        if _after > POST_PAUSE_L1_N:
            # 跳距取**固定序列**（不抽 rng）：见 POST_PAUSE_JUMP_SEQ 的注释
            dist = POST_PAUSE_JUMP_SEQ[_jump_i % len(POST_PAUSE_JUMP_SEQ)] * dt
            _jump_i += 1
            _frozen = True
        elif _after > 0:
            dist = dist * PAUSE_RECOVER_SCALE
            _l1 = True
        walker.advance(dist)
        offset += dt
        traveled += dist
        lng, lat = walker.point()
        # 丢帧：这一秒没拿到 GPS fix ⇒ 不落点，但**路程与时间都不许丢**：攒到下一个落点上
        # （真机上表现为 t 间隔翻倍，而这个点的距离正是整段的和）。
        # ⚠️ 停顿后的那 13 个点期间**不丢帧**：真机那一段是连续的 13 个点，断一个就不成组了。
        if (sample_drop_p > 0 and _after <= 0
                and traveled < _target - 1e-9 and rng.random() < sample_drop_p):
            pend_dist += dist
            pend_dt += dt
            continue
        dist += pend_dist
        dt += pend_dt
        pend_dist = 0.0
        pend_dt = 0.0
        spd = dist / dt if dt > 0 else 0.0   # 吸掉丢帧后取**区间均速** ⇒ 仍满足 distance == speed×dt
        t_ms = start_ms + offset * 1000.0
        if clock_jitter_p > 0 and rng.random() < clock_jitter_p:
            t_ms += rng.choice((-1.0, 1.0))          # 偶发 ±1 ms 时钟抖动
        samples.append({
            "lng": lng, "lat": lat, "t_ms": t_ms,
            "dt_ms": dt * 1000.0, "offset_s": offset, "distance_m": dist,
            "cumulative_m": traveled, "speed_mps": spd, "paused": False,
            "frozen": _frozen, "l1": _l1,
        })
        if _after > 0:
            _after -= 1
        # 真实感：偶发重复点（真机 dt==0：**同一个 t、同一坐标、s=0**）
        if dup_p > 0 and traveled < target_m - 1e-9 and rng.random() < dup_p:
            _last = samples[-1]
            samples.append({
                "lng": _last["lng"], "lat": _last["lat"], "t_ms": _last["t_ms"],
                "dt_ms": 0.0, "offset_s": _last["offset_s"], "distance_m": 0.0,
                "cumulative_m": _last["cumulative_m"], "speed_mps": 0.0, "paused": False,
            })
    if pause_tail_points > 0:
        for s in samples[-pause_tail_points:]:
            s["paused"] = True
    # 计时停止后的收尾段：人沿路线继续走 ~stop_tail_s 秒（位置在动、s 非零），但计时已停。
    # 真机合格记录里这一段是 33 个点：``i="true"``、``l="2"``、``c``/``e`` 冻结。
    if stop_tail_s and float(stop_tail_s) > 0 and traveled >= _target - 1e-9 and samples:
        _left = float(stop_tail_s)
        _v0 = float(stop_walk_mps)
        while _left > 0.05:
            _dt = min(sample_interval_s, _left)
            _v = _v0 * max(0.7, min(1.3, rng.gauss(1.0, 0.12)))
            _dist = _v * _dt
            walker.advance(_dist)
            offset += _dt
            _left -= _dt
            _lng, _lat = walker.point()
            samples.append({
                "lng": _lng, "lat": _lat, "t_ms": start_ms + offset * 1000.0,
                "dt_ms": _dt * 1000.0, "offset_s": offset, "distance_m": _dist,
                "cumulative_m": traveled, "speed_mps": _v, "paused": False,
                "post_stop": True,
            })
    # ⚠️ **有效运动秒**（`c` / sportTime 的口径）：真机合格记录里 `endTime-startTime` = 1248.816 s
    #    而 `sportTime` = 1163 —— 差的 85.8 s 是中途暂停/停帧。⇒ 暂停与重复点都**不能推进**
    #    这个计数（它们只撑长墙上时间）。旧实现让 `c` 跟着 `offset_s` 走，于是 sportTime 把
    #    暂停也算进去、`sportRange/sportTime` 的均速被系统性拉低。
    _mv = 0.0
    for s in samples:
        if (not s.get("paused") and not s.get("post_stop") and not s.get("frozen")
                and float(s.get("dt_ms") or 0.0) > 0.0):
            _mv += float(s["dt_ms"]) / 1000.0
        s["moving_s"] = _mv
    # 起点丢点（B1）：真机计时先跑了 ~4 s 才落第一个点 ⇒ 导出首点 `c`="5"、`e`="0"。
    # ⚠️ 必须**先算完 moving_s 再切**，否则计数从头开始、`c` 又回到 1。
    if start_skip_s and float(start_skip_s) > 0:
        _kept = [s for s in samples if float(s.get("moving_s") or 0.0) > float(start_skip_s)]
        if len(_kept) >= 2:
            samples = _kept
            _rebalance_distance(samples, target_m)
    return samples


# --------------------------------------------------------------------------- 步数分桶（60s）
def allocate_steps(samples: Sequence[Dict[str, Any]],
                   bucket_s: float = BUCKET_SECONDS, sex: str = "male",
                   stride_m: Optional[float] = None, stride_jitter: float = 0.0,
                   rng: Optional[random.Random] = None) -> Dict[str, Any]:
    if bucket_s <= 0:
        raise TrackError("bucket_s 必须为正")
    count = 1
    for s in samples:
        count = max(count, int(s["offset_s"] // bucket_s) + 1)
    acc = [0.0] * count
    for s in samples:
        # ⚠️ 计时停止后的收尾段（`post_stop`）**不计步**：真机收尾 33 点里 e 冻结不动。
        if s.get("post_stop"):
            continue
        idx = min(count - 1, int(s["offset_s"] // bucket_s))
        acc[idx] += steps_for(s["distance_m"], s["speed_mps"], sex=sex,
                              stride_m=stride_m, stride_jitter=stride_jitter, rng=rng)
    total = int(round(sum(acc)))
    buckets = _largest_remainder(acc, total)
    return {"buckets": buckets, "total_steps": sum(buckets), "bucket_seconds": bucket_s}


# --------------------------------------------------------------------------- 轨迹点（两套结构）
def to_legacy_points(samples: Sequence[Dict[str, Any]],
                     crs: str = "gcj02", coord_decimals: int = 7,
                     speed_decimals: int = 2) -> List[Dict[str, Any]]:
    """老链路 LatLngTimeSpeed（未混淆键名，见 17 §6）。"""
    out: List[Dict[str, Any]] = []
    for s in samples:
        lng, lat = _to_crs(s["lng"], s["lat"], crs)
        out.append({
            "latitude": round(lat, coord_decimals),
            "longitude": round(lng, coord_decimals),
            "time": int(s["t_ms"]),
            "speed": round(s["speed_mps"], speed_decimals),
            "isPuase": bool(s["paused"]) or bool(s.get("post_stop")),
            "locationType": 1,
            "countTime": int(s["offset_s"]),
        })
    return out


def to_point_data(samples: Sequence[Dict[str, Any]],
                  crs: str = "gcj02", coord_decimals: int = 6,
                  speed_decimals: int = 6,
                  total_steps: Optional[int] = None) -> List[Dict[str, Any]]:
    """新链路 PointData（单字母键，见 17 §6；结构用**真机合格记录**逐字段校准）。

    与真机对齐的四处硬约定（2026-09-24 拿真机记录逐字段核过）：

      * **9 个键** ``a,c,e,i,l,m,o,s,t``，且**值全为字符串**（坐标 / 速度 6 位小数）；
      * ``l`` = 定位类型（真机常态 ``"2"``、暂停段 ``"3"``），``m`` 恒 ``"0"``；
      * ``e`` = **步数传感器成批上报**的阶梯值：累计到阈值才跳一次（真机 63% 的点 e 不变，
        跳的时候一次 +4~7），不是逐点平滑递增；
      * **尾部约定**（同参照实现）：末两点 ``i`` 置 ``"true"``、末点 ``c``/``e`` 复制前一点。
    """
    out: List[Dict[str, Any]] = []
    pending = 0.0                       # 传感器累计步数（浮点）
    emitted = 0                         # 已上报的步数（阶梯值）
    for i, s in enumerate(samples):
        lng, lat = _to_crs(s["lng"], s["lat"], crs)
        _post = bool(s.get("post_stop"))
        if not _post and not s.get("frozen"):
            # 停顿后「仍冻结」的点（GPS 重捕获大跳）不推进步数 —— 真机那 4 个点 e 也是冻结的
            pending += steps_for(s["distance_m"], s["speed_mps"])
        # 阈值做 ±1.5 的**确定性**抖动（不引入外部随机 ⇒ 同 seed 可复现），
        # 让每次跳变的增量落在真机的 4~7 区间里。
        thr = STEP_BURST_THRESHOLD + ((i * 2654435761) % 7 - 3) * 0.5
        if pending - emitted >= thr:
            emitted = int(pending)
        if i == 0:
            # 真机首点 e="0"（起步那一刻步数传感器还没上报）—— 不扣 pending，
            # 只是这一格不刷新，后续点会把它补回去。
            emitted = 0
        paused = bool(s["paused"])
        out.append({
            "a": "%.*f" % (coord_decimals, round(lat, coord_decimals)),
            "c": str(int(s.get("moving_s", s["offset_s"]))),
            "e": str(int(round(emitted))),
            # 收尾段（计时已停、人还在走）与停顿后「仍冻结」段 i="true" 但 **l 仍是 "2"**；
            # 停顿段 l="3"；停顿后的恢复段 l="1"（真机 3 / 9 / 4 点的三态，逐点核过）。
            "i": "true" if (paused or _post or s.get("frozen")) else "false",
            "l": (POINT_LOCATION_TYPE_PAUSED if paused else
                  (POINT_LOCATION_TYPE_RECOVER if s.get("l1") else POINT_LOCATION_TYPE)),
            "m": POINT_M,
            "o": "%.*f" % (coord_decimals, round(lng, coord_decimals)),
            "s": "%.*f" % (speed_decimals, round(s["speed_mps"], speed_decimals)),
            "t": str(int(s["t_ms"])),
        })
    if len(out) >= 2:
        # 尾部约定（照真机末两点）：c / e 都钉在**最终值**、i="true"。
        # e 的终值必须 == 上传表单的 `step` == Σ stepNumbers（真机 2401/2401/2401 三项相等）——
        # 点数据里的阶梯是拿**坐标重算后的速度**累计的，与步数桶不是同一条链，所以这里以步数桶为准。
        total_e = str(int(round(pending))) if total_steps is None else str(int(total_steps))
        _raw_total = pending
        _last_move = 0
        if total_steps is not None and _raw_total > 0:
            _f = float(total_steps) / _raw_total
            _prev = 0
            for _i, _p in enumerate(out):
                if samples[_i].get("post_stop") or samples[_i].get("frozen"):
                    continue                    # 收尾段 / 停顿后冻结段稍后统一钉到最终值
                _v = int(round(int(_p["e"]) * _f))
                _prev = _v if _v > _prev else _prev
                _p["e"] = str(_prev)
                _last_move = _i
        # 收尾段（计时已停、人还在走）：e **冻结在最终步数**（真机末 33 点 e 恒 2401
        # == step == ΣstepNumbers）；连同最后一个运动点一起钉，避免末尾残留 2400 阶梯。
        for _p in out[_last_move:]:
            _p["e"] = total_e
        last_c = out[-1]["c"]
        out[-1]["e"] = out[-2]["e"] = total_e
        out[-1]["c"] = out[-2]["c"] = last_c
        out[-1]["i"] = out[-2]["i"] = "true"
    return out


# --------------------------------------------------------------------------- 人脸
def normalize_middle_faces(raw: Optional[Sequence[Any]]) -> List[Any]:
    faces: List[Any] = []
    for item in raw or []:
        if isinstance(item, dict):
            faces.append(item)
            continue
        try:
            r = float(item)
        except (TypeError, ValueError):
            continue
        faces.append(max(FACE_RATIO_MIN, min(FACE_RATIO_MAX, r)))
    return faces


def build_face_points(samples: Sequence[Dict[str, Any]], start_ms: int, end_ms: int,
                      middle_faces: Optional[Sequence[Any]] = None,
                      start_face: bool = False, end_face: bool = False,
                      crs: str = "gcj02") -> List[Dict[str, Any]]:
    """faceType: 1=开始 2=中途 3=结束（见 19-参考实现调研 §4）。"""
    faces = normalize_middle_faces(middle_faces)
    pts: List[Dict[str, Any]] = []

    def add(ftype: int, lng: float, lat: float, t_ms: int) -> None:
        lng, lat = _to_crs(lng, lat, crs)
        pts.append({"type": ftype, "latitude": round(lat, 7), "longitude": round(lng, 7),
                    "coordinates": "%.7f,%.7f" % (lat, lng), "timestamp": int(t_ms)})

    if start_face and samples:
        s0 = samples[0]
        add(1, s0["lng"], s0["lat"], start_ms)
    span = max(0, int(end_ms) - int(start_ms))
    for face in faces:
        if isinstance(face, dict):
            t = face.get("timestamp")
            lat, lng = face.get("latitude"), face.get("longitude")
            if t is None or lat is None or lng is None:
                continue
            pts.append({"type": 2, "latitude": float(lat), "longitude": float(lng),
                        "coordinates": "%s,%s" % (lat, lng), "timestamp": int(t)})
            continue
        target = int(start_ms) + int(span * float(face))
        chosen = samples[-1]
        for s in samples:
            chosen = s
            if s["t_ms"] >= target:
                break
        add(2, chosen["lng"], chosen["lat"], chosen["t_ms"])
    if end_face and samples:
        se = samples[-1]
        add(3, se["lng"], se["lat"], int(end_ms))
    pts.sort(key=lambda x: x["timestamp"])
    dedup: List[Dict[str, Any]] = []
    for p in pts:
        if dedup and p["timestamp"] - dedup[-1]["timestamp"] < FACE_MIN_GAP_S * 1000:
            if p["type"] in (1, 3):          # 首尾人脸优先保留
                dedup[-1] = p
            continue
        dedup.append(p)
    return dedup


# --------------------------------------------------------------------------- 合格性
def _as_int(v: Any, d: int = 0) -> int:
    try:
        return d if v in (None, "") else int(float(v))
    except (TypeError, ValueError):
        return d


def validity_checks(setting: Dict[str, Any], elapsed_s: float, distance_m: float, steps: int,
                    avg_speed_mps: float = 0.0, stride_m: float = 0.0,
                    cadence_spm: float = 0.0) -> List[Dict[str, Any]]:
    """逐项列出「合格性」判定（D 合规自检：生成时就告诉你哪条不达标）。

    取值域 / 单位（对齐 `RunRoleSetBean`，见 17 §5.4）：
      ``totalRange``(km) · ``step``(步) · ``startQualifiedTime``/``endQualifiedTime``(分) ·
      ``limitSpeed``(m/s，**全程均速上限**) · ``limitStride``(m，步幅上限) ·
      ``limitStepFrequency``(步/分，步频上限)。限值 ≤0 或缺失 = 不检查（不误判）。
    """
    total = float(setting.get("totalRange", 0) or 0) * 1000.0
    step = _as_int(setting.get("step"), 0)
    is_rec = _as_int(setting.get("isStepRecursion"), 0)
    start_q = _as_int(setting.get("startQualifiedTime"), 0)
    end_q = _as_int(setting.get("endQualifiedTime"), -1)
    req_steps = step
    if is_rec and total > 0 and distance_m > total:
        req_steps = int(math.ceil(step * distance_m / total))
    aq = end_q
    if end_q != -1 and total > 0 and distance_m > total:
        aq = int(math.ceil(end_q * distance_m / total))
    ok_time = elapsed_s >= start_q * 60
    if not ok_time and aq != -1:
        ok_time = elapsed_s <= aq * 60
    lim_speed = float(setting.get("limitSpeed", 0) or 0)
    # 记录里落库的 limitStride / limitStepFrequency，源头是 setting 的
    # **endStride / endLimitStepFrequency**（真机 setting 里根本没有 limitStride 这两个键）
    lim_stride = float(setting.get("limitStride", 0) or setting.get("endStride", 0) or 0)
    lim_cad = float(setting.get("limitStepFrequency", 0)
                    or setting.get("endLimitStepFrequency", 0) or 0)
    return [
        {"name": "距离", "ok": total <= 0 or distance_m >= total,
         "detail": "%.1f / %.1f m" % (distance_m, total)},
        {"name": "时长", "ok": ok_time,
         "detail": "%.0f s（≥%d min%s）" % (elapsed_s, start_q,
                                            "" if aq == -1 else " 且 ≤%d min" % aq)},
        {"name": "步数", "ok": req_steps <= 0 or steps >= req_steps,
         "detail": "%d / %d 步" % (steps, req_steps)},
        {"name": "限速", "ok": lim_speed <= 0 or avg_speed_mps <= lim_speed + 1e-9,
         "detail": "%.3f / %.3f m/s" % (avg_speed_mps, lim_speed) if lim_speed > 0 else "未设限"},
        {"name": "限步幅", "ok": lim_stride <= 0 or stride_m <= lim_stride + 1e-9,
         "detail": "%.3f / %.3f m" % (stride_m, lim_stride) if lim_stride > 0 else "未设限"},
        {"name": "限步频", "ok": lim_cad <= 0 or cadence_spm <= lim_cad + 1e-9,
         "detail": "%.1f / %.1f 步/分" % (cadence_spm, lim_cad) if lim_cad > 0 else "未设限"},
    ]


def is_valid_qualified(setting: Dict[str, Any], elapsed_s: float,
                       distance_m: float, steps: int,
                       avg_speed_mps: float = 0.0, stride_m: float = 0.0,
                       cadence_spm: float = 0.0) -> int:
    checks = validity_checks(setting, elapsed_s, distance_m, steps,
                             avg_speed_mps=avg_speed_mps, stride_m=stride_m,
                             cadence_spm=cadence_spm)
    return 1 if all(c["ok"] for c in checks) else 0


# --------------------------------------------------------------------------- 指标
def fmt_time(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s)


def fmt_range(meters: float) -> str:
    return "%.3f" % (math.floor(meters / 1000.0 * 1000) / 1000.0)


def fmt_pace(meters: float, sec: float) -> str:
    # 撇号用 **Unicode ′ ″**（U+2032/U+2033）—— 真机记录里就是这两个字符，
    # 用 ASCII 的 ' " 会和真机对不上（服务器按原样存我们发上去的字符串）。
    if meters <= 0:
        return "0\u20320\u2033"
    spk = sec / (meters / 1000.0)
    return "%d\u2032%02d\u2033" % (int(spk // 60), int(round(spk % 60)))


def fmt_avg(meters: float, sec: float) -> str:
    if meters <= 0 or sec <= 0:
        return "0.0"
    return "%.2f" % ((meters / 1000.0) / (sec / 3600.0))


def summarize(samples: Sequence[Dict[str, Any]], buckets: Dict[str, Any],
              sex: str = "male", stride_m: Optional[float] = None) -> Dict[str, Any]:
    # ⚠️ 里程只算**计时中的**样本：真机收尾 33 点（计时已停、人还在走）不进 sportRange
    #    （真机折线长 3209 m 但 sportRange 写的是 3.110 km）。
    dist = sum(s["distance_m"] for s in samples if not s.get("post_stop"))
    wall = float(samples[-1]["offset_s"]) if samples else 0.0
    # 运动时长：与 build_submit / 点数据 `c` 同一口径（不含暂停）
    elapsed = float(samples[-1].get("moving_s", wall)) if samples else 0.0
    avg = dist / elapsed if elapsed > 0 else 0.0
    nsteps = buckets["total_steps"] or 0
    # ⚠️ 步幅要用**聚合**口径（里程 / 总步数），不是「速度取均值后的点估」：
    #    服务器存下来的 stride = sportRange / step（真机 3110 m / 2401 步 = 1.295 m = 129 cm）。
    #    点估（0.75+0.15·v̄）会给出 1.147 m —— 和它自己的 steps 字段自相矛盾（同里程步数
    #    更多却步幅更短），一算步频就冒到 138 步/分，而真机是 124。
    if stride_m and float(stride_m) > 0:
        st = float(stride_m)
    elif nsteps > 0:
        st = dist / nsteps
    else:
        st = stride_for_speed(avg, sex)
    # 停顿次数按**组**算（连续 paused 采样算一次）：真机形态下停顿被落成 3 个点，
    # 逐点计数会把「1 次停顿」误报成 3 次。
    _pc, _prev_p = 0, False
    for s in samples:
        _p = bool(s.get("paused"))
        if _p and not _prev_p:
            _pc += 1
        _prev_p = _p
    return {
        "distance_m": round(dist, 2),
        "elapsed_s": round(elapsed, 1),
        "wall_s": round(wall, 1),
        "point_count": len(samples),
        "avg_speed_mps": round(avg, 3),
        "avg_speed_kmh": round(avg * 3.6, 3),
        "pace_s_per_km": round(elapsed / (dist / 1000.0), 1) if dist > 0 else 0.0,
        "pace": fmt_pace(dist, elapsed),
        "steps": nsteps,
        "stride_m": round(st, 3),
        "cadence_spm": round(avg / st * 60.0, 1) if avg > 0 else 0.0,
        "bucket_count": len(buckets["buckets"]),
        "pause_count": _pc,
        "pause_s": round(sum(s["dt_ms"] for s in samples if s.get("paused")) / 1000.0, 1),
    }


# --------------------------------------------------------------------------- 围栏 / 相似度（D 合规自检）
def _as_latlng(p: Any) -> Optional[Tuple[float, float]]:
    """吃 {lat,lng} / {latitude,longitude} / [lng,lat] / [lat,lng]，统一成 (lat, lng)。"""
    if isinstance(p, dict):
        lat = p.get("lat", p.get("latitude"))
        lng = p.get("lng", p.get("longitude"))
        if lat is None or lng is None:
            return None
        try:
            return float(lat), float(lng)
        except (TypeError, ValueError):
            return None
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        try:
            a, b = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            return None
        # 纬度必在 ±90：若第一个数超界，说明是 [lng,lat]
        return (a, b) if abs(a) <= 90 else (b, a)
    return None


def _point_in_ring(lat: float, lng: float, ring: Sequence[Tuple[float, float]]) -> bool:
    """射线法：点是否在单个多边形内（支持任意简单多边形）。"""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (xi > lng) != (xj > lng):
            if lat < (yj - yi) * (lng - xi) / (xj - xi) + yi:
                inside = not inside
        j = i
    return inside


def check_geofence(samples: Sequence[Dict[str, Any]],
                   geofence: Any) -> Optional[Dict[str, Any]]:
    """所有采样点是否都落在电子围栏内。``geofence`` 形如 ``[[{lat,lng},...], ...]``
    （``17-接口规格书`` §5.4 的 ``RunRoleSetBean.geofence``）。没给围栏就返回 ``None``。"""
    if not geofence:
        return None
    polys: List[List[Tuple[float, float]]] = []
    for poly in geofence:
        if isinstance(poly, dict) or not isinstance(poly, (list, tuple)):
            poly = [poly]
        ring = [q for q in (_as_latlng(p) for p in poly) if q]
        if len(ring) >= 3:
            polys.append(ring)
    if not polys:
        return None
    outside = 0
    worst = None
    for s in samples:
        lat, lng = float(s["lat"]), float(s["lng"])
        if not any(_point_in_ring(lat, lng, r) for r in polys):
            outside += 1
            if worst is None:
                worst = [round(lng, 7), round(lat, 7)]
    return {"ok": outside == 0, "points": len(samples), "outside": outside,
            "polygons": len(polys), "first_outside": worst}


def _grid_cells(pts: Sequence[Tuple[float, float]], cell_m: float) -> Dict[Tuple[int, int], List[Tuple[float, float]]]:
    g: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
    for lat, lng in pts:
        key = (int(math.floor(lat * M_PER_DEG_LAT / cell_m)),
               int(math.floor(lng * m_per_deg_lng(lat) / cell_m)))
        g.setdefault(key, []).append((lat, lng))
    return g


def _covered(a: Sequence[Tuple[float, float]], grid: Dict[Tuple[int, int], List[Tuple[float, float]]],
             cell_m: float, tol_m: float) -> float:
    """a 里有多少比例的点，能在 b 的网格里找到 ≤ tol_m 的邻居。"""
    if not a:
        return 0.0
    hit = 0
    for lat, lng in a:
        cx = int(math.floor(lat * M_PER_DEG_LAT / cell_m))
        cy = int(math.floor(lng * m_per_deg_lng(lat) / cell_m))
        found = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (bl, bg) in grid.get((cx + dx, cy + dy), ()):
                    if haversine(lat, lng, bl, bg) <= tol_m:
                        found = True
                        break
                if found:
                    break
            if found:
                break
        if found:
            hit += 1
    return hit / float(len(a))


def path_overlap(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]],
                 tol_m: float = 5.0) -> float:
    """两条折线的贴近程度 0~1：A 的点被 B 覆盖比例与 B 被 A 覆盖比例的均值。
    ``a``/``b`` 接受 [{lat,lng}] 或 [[lng,lat]]；1.0 ≈ 同一条线（长度也接近时）。"""
    pa = [q for q in (_as_latlng(p) for p in a) if q]
    pb = [q for q in (_as_latlng(p) for p in b) if q]
    if len(pa) < 2 or len(pb) < 2:
        return 0.0
    cell = max(tol_m, 1.0)
    ga, gb = _grid_cells(pa, cell), _grid_cells(pb, cell)
    return round(0.5 * (_covered(pa, gb, cell, tol_m) + _covered(pb, ga, cell, tol_m)), 4)


# --------------------------------------------------------------------------- 校验
def validate(samples: Sequence[Dict[str, Any]], buckets: Dict[str, Any],
             target_m: float, sex: str = "male",
             stride_m: Optional[float] = None) -> Dict[str, Any]:
    issues: List[Dict[str, str]] = []

    def err(code: str, msg: str) -> None:
        issues.append({"code": code, "message": msg})

    if len(samples) < 2:
        err("too_few_points", "采样点少于 2 个")
        return {"ok": False, "issues": issues}
    for i in range(1, len(samples)):
        # 允许「同一毫秒」——真机就有 dt==0 的重复点（同 t / 同坐标 / s=0）；
        # 只把**时间倒流**当错误。
        if samples[i]["t_ms"] - samples[i - 1]["t_ms"] < 0:
            err("time_reversed", "第 %d 点时间戳倒流" % i)
    for i, s in enumerate(samples):
        exp = s["speed_mps"] * (s["dt_ms"] / 1000.0)
        if abs(s["distance_m"] - exp) > max(0.5, abs(exp) * 0.001):
            err("speed_distance_mismatch",
                "第 %d 点 |distance-speed×dt| 超容差 (%.6f vs %.6f)" % (i, s["distance_m"], exp))
    dist = sum(s["distance_m"] for s in samples)
    if dist > target_m + 0.5:
        err("target_exceeded", "实际距离 %.3f m 超过目标 %.3f m" % (dist, target_m))
    if sum(buckets["buckets"]) != buckets["total_steps"]:
        err("bucket_sum_mismatch", "步数桶合计 != 总步数")
    bucket_sum = sum(buckets["buckets"])
    # ⚠️ 估算必须用**与分桶同一套**步幅参数（性别 / 显式步幅）：否则 `stride_jitter` 一开
    #    （分桶时抖了、这里按不抖动重估）就必然假报 bucket_total_mismatch。
    #    容差 = 分桶取整误差（每桶 ≤0.5） + 1% 抖动余量；口径真错（步幅模型不对）照样抓得住。
    est = int(round(sum(steps_for(s["distance_m"], s["speed_mps"], sex=sex,
                                  stride_m=stride_m) for s in samples)))
    tol = max(1 + len(buckets["buckets"]) // 2, int(round(bucket_sum * 0.01)))
    if abs(bucket_sum - est) > tol:
        err("bucket_total_mismatch",
            "步数桶合计 %d 与逐步估算 %d 不一致（容差 %d）" % (bucket_sum, est, tol))
    return {"ok": not issues, "issues": issues}


# --------------------------------------------------------------------------- 坐标变换
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lng: float, lat: float) -> bool:
    return not (72.004 < lng < 137.8347 and 0.8293 < lat < 55.8271)


def _tf_lat(x: float, y: float) -> float:
    r = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _tf_lng(x: float, y: float) -> float:
    r = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def wgs84_to_gcj02(lng: float, lat: float) -> Tuple[float, float]:
    if _out_of_china(lng, lat):
        return lng, lat
    dlat, dlng = _tf_lat(lng - 105.0, lat - 35.0), _tf_lng(lng - 105.0, lat - 35.0)
    rlat = lat / 180.0 * math.pi
    m = math.sin(rlat)
    m = 1 - _EE * m * m
    sm = math.sqrt(m)
    dlat = dlat * 180.0 / ((_A * (1 - _EE)) / (m * sm) * math.pi)
    dlng = dlng * 180.0 / (_A / sm * math.cos(rlat) * math.pi)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng: float, lat: float) -> Tuple[float, float]:
    if _out_of_china(lng, lat):
        return lng, lat
    glng, glat = wgs84_to_gcj02(lng, lat)
    return lng * 2 - glng, lat * 2 - glat


def _to_crs(lng: float, lat: float, crs: str) -> Tuple[float, float]:
    if crs == "gcj02":
        return wgs84_to_gcj02(lng, lat)
    return lng, lat


# --------------------------------------------------------------------------- 路线导入 / 导出（GPX）
def parse_gpx(text: str) -> List[List[float]]:
    """从 GPX 文本抽出折线 [[lng,lat], ...]（先 trkpt，无则 rtept，再 wpt）。

    只取经纬度（GPX 是 WGS-84）；忽略时间/海拔。用 xml.etree 按局部名匹配，
    不依赖命名空间前缀。
    """
    import re as _re
    import xml.etree.ElementTree as ET

    txt = (text or "").lstrip("\ufeff").strip()
    if not txt:
        raise TrackError("GPX 内容为空")
    try:
        root = ET.fromstring(txt)
    except ET.ParseError as e:
        raise TrackError("GPX 不是合法 XML: %s" % e) from e

    def lname(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    order = ("trkpt", "rtept", "wpt")
    found: List[Any] = []
    for kind in order:
        found = [el for el in root.iter() if lname(el.tag) == kind]
        if found:
            break
    pts: List[List[float]] = []
    for el in found:
        try:
            lat = float(el.get("lat"))
            lng = float(el.get("lon"))
        except (TypeError, ValueError):
            m = _re.search(r"\b(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\b", el.text or "")
            if not m:
                continue
            lat, lng = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            continue
        pts.append([lng, lat])
    if len(pts) < 2:
        raise TrackError("GPX 里没找到足够的轨迹点（需 ≥ 2，trkpt/rtept/wpt）")
    return normalize_path(pts)


def to_gpx(points: Sequence[Sequence[float]], name: str = "route",
           crs: str = "wgs84") -> str:
    """把 [[lng,lat],...] 导成 GPX 1.1 文本（默认输出 WGS-84；`crs="gcj02"` 则先反解）。"""
    pts = normalize_path(points)
    out: List[Tuple[float, float]] = []
    for lng, lat in pts:
        out.append(gcj02_to_wgs84(lng, lat) if crs == "gcj02" else (lng, lat))
    safe_name = (name or "route").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    head = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="route_studio" '
            'xmlns="http://www.topografix.com/GPX/1/1">\n'
            '  <trk>\n    <name>%s</name>\n    <trkseg>\n' % safe_name)
    body = "".join('      <trkpt lat="%.7f" lon="%.7f"></trkpt>\n' % (lat, lng) for lng, lat in out)
    return head + body + "    </trkseg>\n  </trk>\n</gpx>\n"


# --------------------------------------------------------------- 跑步类型编号（唯一出处）
# 真相来自参考实现 `tsnRunServer`（2026-09-24 实测坐实）：
#   · 新链路（学校私服 v2）：`sportType` / `runType` = **0 自由跑 · 1 阳光跑 · 2 晨跑（早操）**
#     —— 参考 `publicRunTypeConvert`：morningRun(旧 1)→2、sumRun(旧 2)→1、freedom(旧 3)→0。
#   · 老链路（v1 `m.boxkj.com`）：`runType` = **1 晨跑 · 2 阳光跑 · 3 自由跑**（旧编号）。
# **本仓对外一律用新编号**（界面 / `/api/*` / `run_type` 参数都是 0/1/2）：发新链路原样发，
# 只有走老链路才用 `v1_run_type()` 换算。
# ⚠️ 2026-09-24 踩过：把「旧→新」表（`{1:2,2:1,3:0}`）用在了**已经是新编号**的 `run_type` 上
# ⇒ 界面选「阳光跑」(1) 被算成「晨跑」(2)，真实服务器回
# `V2Error(v2 999: HTTP 999 晨跑未开启)`（配置那步却过了 —— 因为配置发的是新编号）。
RUN_TYPE_NAMES = {0: "自由跑", 1: "阳光跑", 2: "晨跑"}
V1_RUN_TYPE_BY_RUN_TYPE = {0: 3, 1: 2, 2: 1}


def run_type_int(v, default: int = 1) -> int:
    """把界面上来的 `run_type` 收成 int（取不到就用 default，别让脏值一路带下去）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def run_type_name(run_type) -> str:
    """新编号 → 中文名（日志 / 诊断 / 提示用）。"""
    return RUN_TYPE_NAMES.get(run_type_int(run_type, -1), "未知")


def v1_run_type(run_type) -> int:
    """新编号 → 老链路（v1）`runType`：1 晨跑 · 2 阳光跑 · 3 自由跑。"""
    return V1_RUN_TYPE_BY_RUN_TYPE.get(run_type_int(run_type, -1), 2)


# --------------------------------------------------------------------------- 上传表单
def build_submit(samples: Sequence[Dict[str, Any]], buckets: Dict[str, Any],
                 faces: Sequence[Dict[str, Any]], identify: str,
                 setting: Dict[str, Any], run_type: int = 1,
                 device: Optional[Dict[str, str]] = None,
                 user_id: str = "20210001", app_version: Optional[str] = None,
                 crs: str = "gcj02", face_status_on: bool = True) -> Dict[str, Any]:
    """产出老链路 appAddSportRecord 的 21 个 @Field（17 §7）。"""
    app_version = app_version or DP.DEFAULT_APP_VERSION      # 默认 = 真机 3.6.814
    dev = {k: DP.DEFAULT_DEVICE[k] for k in ("brand", "model", "system", "version")}
    dev.update({k: v for k, v in (device or {}).items()
                if k in ("brand", "model", "system", "version") and v})
    track = to_legacy_points(samples, crs=crs)
    dist = sum(s["distance_m"] for s in samples if not s.get("post_stop"))
    # ⚠️ 墙上时间（endTime）与运动时长（sportTime）是**两个量**：真机 end-start=1248.8 s 而
    #    sportTime=1163 s。用时/均速/配速一律按**运动时长**算，endTime 才用墙上时间。
    wall = float(samples[-1]["offset_s"]) if samples else 0.0
    elapsed = int(round(float(samples[-1].get("moving_s", wall)))) if samples else 0
    start_ms = int(samples[0]["t_ms"]) if samples else 0
    end_ms = start_ms + int(round(wall * 1000.0))
    return {
        "userId": str(user_id),
        "runType": str(run_type),      # 用**本仓新编号**；走老链路时由调用方换成 v1 编号
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "gitudeLatitude": _dumps(track),
        "identify": identify,
        "formatSportTime": fmt_time(elapsed),
        "formatSportRange": fmt_range(dist),
        "avgspeed": fmt_avg(dist, elapsed),
        "speed": fmt_pace(dist, elapsed),
        "okPointList": "[]",
        "points": "[]",
        "brand": dev["brand"], "model": dev["model"],
        "system": dev["system"], "version": dev["version"],
        "appVersion": app_version,
        "stepNumbers": _dumps(buckets["buckets"]),
        "isFaceStatus": "1" if face_status_on else "0",
        "uploadType": "1",
        "keepId": "",
    }


def _dumps(o: Any) -> str:
    import json
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- 一键
def _rebalance_distance(samples: List[Dict[str, Any]], target_m: float) -> None:
    """把「计时中」样本的里程合计**配平到 `target_m`**（B1 起步丢点用）。

    丢点会把起步那几秒的 ~10 m 里程一起丢掉 ⇒ `formatSportRange` 会写成 3.099 而不是 3.110。
    差额**均摊**到每个计时中的样本（每点只动 ~0.01 m），同时把 `speed_mps` 按 `dist/dt` 重算、
    `cumulative_m` 重新累加 ⇒ `distance == speed×dt` 与本仓的自校验都不破。

    * 暂停点（`paused`，distance/speed 恒 0）与收尾段（`post_stop`，本就不进 sportRange）
      **不参与**配平 —— 前者是 A26 用例钉死的，后者是 summarize/build_submit 的口径。
    """
    live = [s for s in samples
            if not s.get("post_stop") and not s.get("paused")]
    if not live:
        return
    diff = float(target_m) - sum(float(s["distance_m"]) for s in live)
    if abs(diff) > 1e-9:
        step = diff / len(live)
        for s in live:
            s["distance_m"] = max(0.0, float(s["distance_m"]) + step)
            dt = float(s.get("dt_ms") or 0.0) / 1000.0
            s["speed_mps"] = (float(s["distance_m"]) / dt) if dt > 0 else 0.0
    acc = 0.0
    for s in samples:
        if not s.get("post_stop"):
            acc += float(s["distance_m"])
        s["cumulative_m"] = acc


def _resync_speed_from_coords(samples: List[Dict[str, Any]]) -> None:
    """把导出层的 `speed_mps` 改成**由导出坐标与时间戳算出来的位移速度**。

    真机上这两者本来就是同一个数：拿本人账号读回 示例记录 实测，
    `s` 与 `|P_i-P_{i-1}|/Δt` 的比值中位 1.000、p10 0.975、p90 1.025 —— 仅剩下
    坐标存 6 位小数的量化误差（±2.5%）。

    不重算的后果：内部「计划速度」是一条平滑曲线，而坐标带着噪声 ⇒ 两者对不上，
    实测比值 p90 1.128（13%）—— 一个服务端拿来算一遭就能看出的现成指纹。

    只改导出层（`export_samples`）的 `speed_mps`：内部 samples 不动 ⇒
    `distance == speed×dt` 自校验 / 合格性 / 指标全不受影响。
    """
    n = len(samples)
    if n < 2:
        return
    for i, s in enumerate(samples):
        a = samples[i - 1] if i > 0 else samples[0]
        b = samples[i] if i > 0 else samples[1]
        dt = (b["t_ms"] - a["t_ms"]) / 1000.0
        if dt <= 0:
            s["speed_mps"] = 0.0
            continue
        dx = (b["lng"] - a["lng"]) * m_per_deg_lng(a["lat"])
        dy = (b["lat"] - a["lat"]) * M_PER_DEG_LAT
        s["speed_mps"] = math.hypot(dx, dy) / dt


def generate(path: Sequence[Any], target_km: float, plan_pace_s_per_km: float,
             start_ms: Optional[int] = None, run_type: int = 1,
             device: Optional[Dict[str, str]] = None, identify: str = "identify-demo",
             setting: Optional[Dict[str, Any]] = None,
             sample_interval_s: float = 1.0, speed_jitter: float = 0.08,
             interval_jitter_s: float = 0.0, loop_mode: str = "repeat",
             track_lateral_m: float = TRACK_LATERAL_M, track_pace_pct: float = TRACK_PACE_PCT,
             coord_jitter_deg: float = 0.0,
             gps_noise_m: float = GPS_NOISE_M, gps_outage_p: float = 0.0, pace_swing: float = 0.0, dup_p: float = 0.0,
             pause_tail_points: int = 0, crs: str = "gcj02",
             sample_drop_p: float = SAMPLE_DROP_P,
             clock_jitter_p: float = CLOCK_JITTER_P,
             warmup_s: float = 0.0, cooldown_s: float = 0.0,
             stop_tail_s: float = 0.0, stop_walk_mps: float = 1.5,
             mid_pauses: Optional[Sequence[Dict[str, Any]]] = None,
             pause_count: int = 0, sex: str = "male",
             stride_m: Optional[float] = None, stride_jitter: float = 0.0,
             start_face: bool = False, end_face: bool = False,
             middle_faces: Optional[Sequence[Any]] = None,
             user_id: str = "20210001", app_version: Optional[str] = None,
             start_skip_s: float = START_SKIP_S, pause_block: bool = True,
             target_jitter_m: float = 0.0,
             seed: Optional[int] = None) -> Dict[str, Any]:
    """地图折线 → 完整跑步记录（轨迹两套结构 + 步数桶 + 人脸 + 合格性 + 上传表单）。

    ``sex`` / ``stride_m`` / ``stride_jitter`` 控制步幅（性别系数 + 可选随机抖动）；
    ``pause_count`` > 0 且未显式给 ``mid_pauses`` 时，随机生成中途暂停。
    """
    import time as _t
    pts = normalize_path(path)
    if len(pts) < 2:
        raise TrackError("至少需要两个坐标点")
    target_m = float(target_km) * 1000.0
    if target_m <= 0:
        raise TrackError("目标距离必须为正")
    if start_ms is None:
        start_ms = int(_t.time() * 1000) + 60000
    rng = random.Random(seed) if seed is not None else random.Random()
    # 总距离抖动（`target_jitter_m`，单位米）：真机 GPS 同样圈数也会报出 ±上百米的差。
    # 在目标上抽一个均匀偏移；夹在有效区间 [0.8, 3.0] km 内，别把「当日有效」跑出界。
    if target_jitter_m and float(target_jitter_m) > 0:
        target_m += rng.uniform(-float(target_jitter_m), float(target_jitter_m))
        # 只在「目标本就落在当天有效区间 0.8–3.0 km」时把抖动夹回区间，别让 ±百米把
        # 有效里程顶出界；用户明确要跑区间外（多跑 >3 km / 少跑 <0.8 km）时不夹。
        if 800.0 <= float(target_km) * 1000.0 <= 3000.0:
            target_m = max(800.0, min(3000.0, target_m))
        else:
            target_m = max(100.0, target_m)
    plan_s = target_m / 1000.0 * float(plan_pace_s_per_km) if plan_pace_s_per_km > 0 else 0.0
    if plan_s <= 0:
        plan_s = target_m / 2.5
    setting = dict(setting or {})
    setting.setdefault("totalRange", round(target_km, 3))
    mid_pauses = list(mid_pauses or [])
    if pause_count and int(pause_count) > 0 and not mid_pauses:
        # 「暂停次数」：位置落在计划用时的 8%~92%、时长 15~45 s（等红灯 / 系鞋带量级），全程随机
        n = max(0, min(20, int(pause_count)))
        mid_pauses = [{"at": round(a, 4), "seconds": round(rng.uniform(15.0, 45.0), 1)}
                      for a in sorted(rng.uniform(0.08, 0.92) for _ in range(n))]
    # 配速档（``pace_swing``）让速度一段快一段慢 —— 但**总用时必须仍等于计划用时**
    # （真机也是均速 = 计划配速）。距离固定时总用时 T = plan_s/(s·m̄)，s = 统一倍率；
    # 故先干跑量出 T1，再令 s ← s·(T1/plan_s)，2~3 轮定点即收敛（实测 |ΔT| < 1%）。
    # 干跑与正式跑共用同一段随机流（getstate/setstate）⇒ 给定 seed 结果可复现。
    _pace_scale = 1.0
    if pace_swing and float(pace_swing) > 0:
        # 定标：让**运动时长**（不含暂停）打回计划用时 plan_s。
        # ⚠️ 不能用 `scale *= t/plan` 的乘性迭代：t(scale) 的弹性≈ -1（实测 dln t/dln scale
        #    ≈ -1.05），乘性更新正好落在振荡区 —— 跑 6 轮实测在 1117↔1211 间来回跳、
        #    永远进不了容差；换成对单调下降的 t(scale) 做**二分**。
        #    每次求值都从同一 rng 状态出发（与最终正式跑同一条随机流）⇒ 可复现。
        _st = rng.getstate()
        _scratch = random.Random()

        def _t_of(sc):
            _scratch.setstate(_st)
            _pp = resample_path(pts, target_m, int(start_ms), plan_s,
                                sample_interval_s=sample_interval_s, speed_jitter=speed_jitter,
                                interval_jitter_s=interval_jitter_s, loop_mode=loop_mode,
                                track_lateral_m=track_lateral_m, track_pace_pct=track_pace_pct,
                                pace_swing=pace_swing, dup_p=dup_p, pace_scale=sc,
                                pause_tail_points=pause_tail_points,
                                sample_drop_p=sample_drop_p, clock_jitter_p=clock_jitter_p,
                                warmup_s=warmup_s, cooldown_s=cooldown_s, mid_pauses=mid_pauses,
                                stop_tail_s=stop_tail_s, stop_walk_mps=stop_walk_mps,
                                start_skip_s=start_skip_s, pause_block=pause_block,
                                rng=_scratch)
            _l = _pp[-1] if _pp else None
            return float((_l or {}).get("moving_s", (_l or {}).get("offset_s", plan_s)))

        _lo, _hi = 0.5, 2.0
        while _lo > 1e-3 and _t_of(_lo) < plan_s:
            _lo *= 0.5
        while _hi < 1e3 and _t_of(_hi) > plan_s:
            _hi *= 2.0
        for _ in range(14):                      # 14 步 ⇒ 区间缩到 1.5/16384 ≈ 0.01%
            _mid = 0.5 * (_lo + _hi)
            if _t_of(_mid) > plan_s:
                _lo = _mid
            else:
                _hi = _mid
        # t(scale) 是**阶梯函数**（小改动会翻转丢帧/重复点的判定，一次跳十几秒），
        # 所以最后在区间两端里挑**更贴近计划用时**的那个 —— 取中点可能落在台阶另一侧。
        _pace_scale = min((_lo, _hi), key=lambda _s: abs(_t_of(_s) - plan_s))
        rng.setstate(_st)
    samples = resample_path(pts, target_m, int(start_ms), plan_s,
                            sample_interval_s=sample_interval_s, speed_jitter=speed_jitter,
                            interval_jitter_s=interval_jitter_s, loop_mode=loop_mode,
                            track_lateral_m=track_lateral_m, track_pace_pct=track_pace_pct,
                            pace_swing=pace_swing, dup_p=dup_p,
                            pace_scale=_pace_scale,
                            pause_tail_points=pause_tail_points,
                            sample_drop_p=sample_drop_p, clock_jitter_p=clock_jitter_p,
                            warmup_s=warmup_s, cooldown_s=cooldown_s, mid_pauses=mid_pauses,
                            stop_tail_s=stop_tail_s, stop_walk_mps=stop_walk_mps,
                            start_skip_s=start_skip_s, pause_block=pause_block,
                            rng=rng)
    # 偶发「GPS 断记」：记录里出现一段**长间隔**（手机丢定位几十秒后重捕获）——
    # 真机 示例记录（1161 点）里就有 1 次 59.7 s 空档、跨空档位移 54 m
    # （≈ 0.9 m/s ⇒ 断记时人在慢走）。**不是每次都有**（概率 `gps_outage_p`）。
    # 只删采样点、不改真实路径 ⇒ 断记边界按**弦长**重算里程/速度，`distance == speed×dt`
    # 自校验不破；总里程只损失「弧 − 弦」（慢速段很小 ≈ 0.5%）。
    outage = None
    if gps_outage_p and float(gps_outage_p) > 0 and len(samples) > 12 \
            and rng.random() < float(gps_outage_p):
        _lo, _hi = len(samples) // 10, len(samples) * 9 // 10
        _live = [i for i in range(_lo, _hi)
                 if not samples[i].get("paused") and not samples[i].get("post_stop")]
        if not _live:
            _live = list(range(_lo, max(_lo + 1, _hi)))
        _i0 = rng.choice(_live)
        _t0 = int(samples[_i0]["t_ms"])
        # 窗口按**弧长**卡：跨度到 `_target_m` 或 60 s 即封口 ⇒ 无论当时快慢，
        # 跨空档位移都落在 20–75 m、时长 15–60 s（像真机），不是跑起来就 150 m。
        _target_m = rng.uniform(GPS_OUTAGE_MIN_M, GPS_OUTAGE_MAX_M)
        _dmin_ms = int(GPS_OUTAGE_MIN_S * 1000.0)
        _dmax_ms = int(GPS_OUTAGE_MAX_S * 1000.0)
        _arc = 0.0
        _i1 = _i0
        for _k in range(_i0 + 1, len(samples)):
            _arc += float(samples[_k].get("distance_m") or 0.0)
            _i1 = _k
            _dtk = int(samples[_k]["t_ms"]) - _t0
            if _dtk >= _dmax_ms or (_dtk >= _dmin_ms and _arc >= _target_m):
                break
        # 断记**两端都必须是「计时中」样本**：若窗口尽头是暂停/收尾点，把它一并吞进空档，
        # 免得给暂停点赋上非 0 里程（会破坏「暂停点 distance 恒 0」与配平口径）。
        while _i1 < len(samples) - 2 and (samples[_i1 + 1].get("paused")
                                          or samples[_i1 + 1].get("post_stop")):
            _i1 += 1
        if _i0 < _i1 < len(samples) - 1 and _arc >= 5.0 \
                and not samples[_i1 + 1].get("paused") and not samples[_i1 + 1].get("post_stop"):
            _kept = samples[:_i0 + 1] + samples[_i1 + 1:]
            _pp, _p = _kept[_i0], _kept[_i0 + 1]
            _ch = haversine(_pp["lat"], _pp["lng"], _p["lat"], _p["lng"])
            _dt = max(1e-6, (int(_p["t_ms"]) - int(_pp["t_ms"])) / 1000.0)
            # 断记边界按**弦长 + 真实间隔**重算（服务端就是这么算的）⇒ `distance == speed×dt` 不破。
            _p["distance_m"] = _ch
            _p["speed_mps"] = _ch / _dt
            _p["dt_ms"] = _dt * 1000.0
            _acc = 0.0
            for _s in _kept:
                if not _s.get("post_stop"):
                    _acc += float(_s.get("distance_m") or 0.0)
                _s["cumulative_m"] = _acc
            outage = {"at_ms": _t0, "duration_s": round(_dt, 1),
                      "dropped": _i1 - _i0, "gap_m": round(_ch, 1)}
            samples = _kept
            # 断记只删采样点、不改真实路径。删点会让「弦长合计」比原路径短（弧 − 弦），
            # 这里把计时中的里程配平回 `target_m` —— 只动 distance_m/速度、**不动坐标**，
            # 所以跨空档那个大跳变还在，但 sportRange 不掉、合格性不受影响。
            _rebalance_distance(samples, target_m)
    # 点级 GPS 量化抖动（可选）：只作用于**导出坐标**，不动内部 samples
    # ⇒ 距离 / 逐点自校验 / 合格性 / 匹配度全都不受影响。
    export_samples = samples
    if coord_jitter_deg and float(coord_jitter_deg) > 0:
        jd = float(coord_jitter_deg)
        export_samples = [dict(s, lng=s["lng"] + rng.uniform(-jd, jd),
                                  lat=s["lat"] + rng.uniform(-jd, jd)) for s in export_samples]
    # 真实感：手机定位的相对抖动（2D 高斯）+ 偶发多径野值 —— 真机点级残差中位 ~0.18 m、
    # p90 ~0.55 m，位移偶有 6~54 m 孤立跳变。只作用于**导出坐标**，不动内部 samples ⇒
    # 距离 / 逐点自校验 / 合格性全不受影响。
    if gps_noise_m and float(gps_noise_m) > 0:
        _sg = float(gps_noise_m)
        _drift_step = _sg * GPS_DRIFT_STEP
        _perp = _sg * GPS_PERP_FRAC
        _along = _sg * GPS_ALONG_FRAC
        # 真机的两项指标分别约束「横向」与「纵向」噪声，量级差 2 倍以上（读回记录实测）：
        #   横向 0.13 m 级 → 点级残差 0.153 m；纵向 0.05 m 级 → 位移/速度只有 ±2.5%。
        # 各向同性噪声只能二选一：取大则位移/速度飘到 ±11%，取小则残差干净得只剩 0.09 m。
        _ux = _uy = 1.0
        _wx = 0.0
        _ex = []
        for _i, s in enumerate(export_samples):
            # 行走方向（向前看一点；重复点/零位移时沿用上一方向）
            _j = _i + 1 if _i + 1 < len(export_samples) else _i
            _k = _i if _i + 1 < len(export_samples) else _i - 1
            if _k >= 0 and export_samples[_j] is not export_samples[_k]:
                _mx = (export_samples[_j]["lng"] - export_samples[_k]["lng"]) * m_per_deg_lng(s["lat"])
                _my = (export_samples[_j]["lat"] - export_samples[_k]["lat"]) * M_PER_DEG_LAT
                _L = math.hypot(_mx, _my)
                if _L > 1e-9:
                    _ux, _uy = _mx / _L, _my / _L
            # 横向慢游走（变道 / 绕人 / 人身体重心的左右摆）：**只加在垂直运动方向**。
            # 这一点很关键：横向位移对「相邻点位移」只有二阶影响（≈ 不发生畸变），
            # 所以它能把点级残差顶到真机量级，却不会把「位移/速度」比值撞飞。
            _wx = GPS_DRIFT_KEEP * _wx + rng.gauss(0.0, _drift_step)
            _ra = rng.gauss(0.0, _along)            # 沿运动方向（纵向）
            _rp = _wx + rng.gauss(0.0, _perp)       # 垂直运动方向（横向）= 慢游走 + 白噪
            _ox = _ra * _ux - _rp * _uy
            _oy = _ra * _uy + _rp * _ux
            if GPS_OUTLIER_P > 0 and rng.random() < GPS_OUTLIER_P:
                _ox += rng.choice((-1.0, 1.0)) * rng.uniform(2.0, GPS_OUTLIER_M)
                _oy += rng.choice((-1.0, 1.0)) * rng.uniform(2.0, GPS_OUTLIER_M)
            _ex.append(dict(s, lng=s["lng"] + _ox / m_per_deg_lng(s["lat"]),
                               lat=s["lat"] + _oy / M_PER_DEG_LAT))
        export_samples = _ex
    # 导出的 `s` 一律**从导出坐标与时间戳重算**（与真机同源；见 _resync_speed_from_coords）——
    # 必须在加噪之后、建点数据/老链路之前。**先拷一份**：没开噪声时 `export_samples is samples`，
    # 直接改会把内部采样的速度也改掉 ⇒ 逐点自校验 |distance-speed×dt| 当场报错。
    export_samples = [dict(s) for s in export_samples]
    _resync_speed_from_coords(export_samples)
    buckets = allocate_steps(samples, sex=sex, stride_m=stride_m,
                             stride_jitter=stride_jitter, rng=rng)
    end_ms = int(export_samples[-1]["t_ms"])
    faces = build_face_points(export_samples, int(start_ms), end_ms,
                              middle_faces=middle_faces, start_face=start_face,
                              end_face=end_face, crs=crs)
    metrics = summarize(samples, buckets, sex=sex, stride_m=stride_m)
    report = validate(samples, buckets, target_m, sex=sex, stride_m=stride_m)
    # D 合规自检：电子围栏（给了 setting.geofence 才查；没配就 None = 不显示）
    fence = check_geofence(export_samples, setting.get("geofence"))
    face_on = bool(start_face or end_face or middle_faces)
    vchecks = validity_checks(setting, metrics["elapsed_s"], metrics["distance_m"], metrics["steps"],
                              avg_speed_mps=metrics["avg_speed_mps"], stride_m=metrics["stride_m"],
                              cadence_spm=metrics["cadence_spm"])
    validity = 1 if all(c["ok"] for c in vchecks) else 0
    form = build_submit(export_samples, buckets, faces, identify, setting, run_type=run_type,
                        device=device, user_id=str(user_id), app_version=app_version,
                        crs=crs, face_status_on=face_on)
    return {
        "metrics": metrics,
        "validate": report,
        "validity": {"isValid": validity, "setting": setting, "checks": vchecks},
        "geofence": fence,
        "steps": buckets,
        "faces": faces,
        "track_legacy": to_legacy_points(export_samples, crs=crs),
        "track_pointdata": to_point_data(export_samples, crs=crs,
                                         total_steps=buckets["total_steps"]),
        "samples": samples,
        # ⚠️ 导出的（**带 GPS 噪声**的）样本：`samples` 是未加噪的内部采样。
        #    中途停止（truncate）重建上传坐标时**必须**用这份带噪的，否则整条丢噪声。
        "export_samples": export_samples,
        "path_used": pts,
        "mid_pauses": mid_pauses,
        "submit": form,
        "crs": crs,
        "loop_mode": loop_mode,
        "gps_outage": outage,
        # 收尾段参数随 gen 一起带回：截断（中途停止）时要照同一套形态补这一段。
        "sample_interval_s": float(sample_interval_s),
        "stop_tail_s": float(stop_tail_s or 0.0),
        "stop_walk_mps": float(stop_walk_mps or 1.5),
    }


# --------------------------------------------------------- 中途停止：截到停下的那一刻
def truncate(gen: Dict[str, Any], cut_ms: int,
             *, crs: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """把一条生成好的轨迹**截到 `cut_ms`**（墙钟毫秒）—— 「中途停止 → 按已跑上传」用。

    只留 `t_ms <= cut_ms` 的采样点，末点按比例插在这条边上；步数桶 / 指标 / 两套轨迹点 /
    上传表单全部按截断后的采样**重算** —— 停在哪儿就算到哪儿，一分不多算。

    * 没跑起来（`cut_ms` 不晚于第一个采样点）返回 `None`（这一条不上传）。
    * 原计划里的「结束人脸」（faceType=3）落在没跑到的那段时，**挪到停止那一刻**
      （真 app 里停跑也得先过结束人脸才交得上去）。
    * 返回值是新 dict（不就地改 `gen`），多两个键：`truncated=True` / `cut_ms`。
    """
    # ⚠️ 用**导出的（带噪）**样本：GPS 噪声只活在 export_samples 里，`gen["samples"]` 是
    #    未加噪的内部采样。以前用错 ⇒ 中途停止上传的坐标整条没噪声（真机一看就假）。
    samples = [dict(s) for s in (gen.get("export_samples") or gen.get("samples") or [])]
    if not samples:
        return None
    cut = int(cut_ms or 0)
    start_ms = int(samples[0]["t_ms"])
    if cut <= start_ms:
        return None
    kept: List[Dict[str, Any]] = []
    tail: Optional[Dict[str, Any]] = None
    for s in samples:
        if int(s["t_ms"]) <= cut:
            kept.append(s)
        else:
            tail = s
            break
    if not kept:
        return None
    last = kept[-1]
    if tail is not None and cut > int(last["t_ms"]):
        span = float(int(tail["t_ms"]) - int(last["t_ms"]))
        r = (cut - int(last["t_ms"])) / span if span > 0 else 0.0
        r = max(0.0, min(1.0, r))
        seg = float(tail.get("distance_m") or 0.0) * r
        kept.append({
            "lng": float(last["lng"]) + (float(tail["lng"]) - float(last["lng"])) * r,
            "lat": float(last["lat"]) + (float(tail["lat"]) - float(last["lat"])) * r,
            "t_ms": cut,
            "dt_ms": cut - int(last["t_ms"]),
            "offset_s": float(last.get("offset_s") or 0.0)
                        + (float(tail.get("offset_s") or 0.0) - float(last.get("offset_s") or 0.0)) * r,
            "moving_s": float(last.get("moving_s") or 0.0)
                        + (float(tail.get("moving_s") or tail.get("offset_s") or 0.0)
                           - float(last.get("moving_s") or 0.0)) * r,
            "distance_m": seg,
            "cumulative_m": float(last.get("cumulative_m") or 0.0) + seg,
            "speed_mps": float(tail.get("speed_mps") or last.get("speed_mps") or 0.0),
            "paused": bool(last.get("paused")),
        })

    # 计时停止后的收尾段：与「跑完自动收尾」**同一套形态**（真机 33 点 —— 人沿路线继续走
    # ~stop_tail_s 秒，位置在动、`c`/`e` 冻结、不计里程）。真机在中途按停时同样会记这一段，
    # 所以截断之后也要补上（否则「中途停止」的件系统性缺尾巴）。
    # 边界：末点本来就是收尾点（已经跑过终点线才按停）就不叠加。
    stop_tail_s = float(gen.get("stop_tail_s") or 0.0)
    stop_walk_mps = float(gen.get("stop_walk_mps") or 1.5)
    _si_s = float(gen.get("sample_interval_s") or 1.0)
    tail_ms = 0.0
    _has_tail = any(s.get("post_stop") for s in kept)
    _path = gen.get("path_used") or []
    if stop_tail_s > 0 and kept and not _has_tail and len(_path) >= 2:
        _lm = str(gen.get("loop_mode") or "repeat")
        _rng = random.Random((int(gen.get("seed") or 0) * 2654435761 + int(cut)) & 0x7FFFFFFF)
        _cum = float(kept[-1].get("cumulative_m") or 0.0)
        _mov = float(kept[-1].get("moving_s") or 0.0)
        _off0 = float(kept[-1].get("offset_s") or 0.0)
        _t0 = int(kept[-1].get("t_ms") or 0)   # 以截断点 t_ms 为基准（它才是权威，不是 start_ms）
        _tail_pts: List[Dict[str, Any]] = []
        try:
            _wk = _Walker(_path, _lm, rng=_rng, lateral_m=0.0)
            _wk.advance(_cum)
            _off, _left = _off0, stop_tail_s
            while _left > 0.05:
                _dt = min(_si_s, _left)
                _v = stop_walk_mps * max(0.7, min(1.3, _rng.gauss(1.0, 0.12)))
                _dd = _v * _dt
                _wk.advance(_dd)
                _off += _dt
                _left -= _dt
                _lng, _lat = _wk.point()
                _tail_pts.append({
                    "lng": float(_lng), "lat": float(_lat),
                    "t_ms": _t0 + (_off - _off0) * 1000.0, "dt_ms": _dt * 1000.0,
                    "offset_s": _off, "distance_m": _dd, "cumulative_m": _cum,
                    "speed_mps": _v, "paused": False, "post_stop": True,
                    "moving_s": _mov,
                })
        except TrackError:
            # `loop_mode="once"` 走到路径尽头就报错 —— 宁可不补尾巴，也不能让上传崩掉。
            _tail_pts = []
        if _tail_pts:
            kept.extend(_tail_pts)
            tail_ms = float((_off - _off0) * 1000.0)

    crs = crs or gen.get("crs") or "gcj02"
    setting = dict((gen.get("validity") or {}).get("setting") or {})
    stride = (gen.get("metrics") or {}).get("stride_m")
    buckets = allocate_steps(kept, stride_m=stride)
    end_ms = int(kept[-1]["t_ms"])
    src_faces = [dict(f) for f in (gen.get("faces") or [])]
    faces = [dict(f) for f in src_faces if int(f.get("timestamp") or 0) <= cut]
    if any(int(f.get("type") or 0) == 3 for f in src_faces):
        faces = [f for f in faces if int(f.get("type") or 0) != 3]
        s = kept[-1]
        lng, lat = _to_crs(s["lng"], s["lat"], crs)
        faces.append({"type": 3, "latitude": round(lat, 7), "longitude": round(lng, 7),
                      "coordinates": "%.7f,%.7f" % (lat, lng), "timestamp": end_ms})
        faces.sort(key=lambda x: int(x.get("timestamp") or 0))
    metrics = summarize(kept, buckets, stride_m=stride)
    # 导出速度一律从坐标重算（与 generate 同一口径；截断后最后一段是插值点，尤其要对齐）
    _resync_speed_from_coords(kept)
    sub = gen.get("submit") or {}
    identify = sub.get("identify") or ("identify-%d" % end_ms)
    try:
        run_type = int(sub.get("runType") or 1)
    except (TypeError, ValueError):
        run_type = 1
    device = {k: sub.get(k) for k in ("brand", "model", "system", "version") if sub.get(k)}
    vchecks = validity_checks(setting, metrics["elapsed_s"], metrics["distance_m"],
                              metrics["steps"], avg_speed_mps=metrics["avg_speed_mps"],
                              stride_m=metrics["stride_m"], cadence_spm=metrics["cadence_spm"])
    out = dict(gen)
    out.update({
        "truncated": True, "cut_ms": cut,
        "stop_tail_ms": tail_ms,
        "samples": kept,
        "export_samples": kept,
        "metrics": metrics,
        "steps": buckets,
        "faces": faces,
        "track_legacy": to_legacy_points(kept, crs=crs),
        "track_pointdata": to_point_data(kept, crs=crs, total_steps=buckets["total_steps"]),
        "geofence": check_geofence(kept, setting.get("geofence")),
        "validity": {"isValid": 1 if all(c["ok"] for c in vchecks) else 0,
                     "setting": setting, "checks": vchecks},
        "submit": build_submit(kept, buckets, faces, identify, setting, run_type=run_type,
                               device=device or None,
                               user_id=str(sub.get("userId") or "20210001"),
                               app_version=sub.get("appVersion"),
                               crs=crs, face_status_on=bool(faces)),
    })
    return out
