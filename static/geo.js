/* ============================================================================
 * geo.js —— 画线几何内核（纯函数，无 DOM 依赖）
 *
 * 为什么单独一个文件：画线精度是这个工作台的命门，必须能被测试。
 * 浏览器里挂到 `window.GEO`；Node 里 `require('./geo.js')`，两边同一份实现
 * （`geo_selftest.js` 就是拿 Node 跑它）。
 *
 * 约定：点一律 `{lat, lng}`，单位为度；距离一律米；`R` 用 IUGG 平均半径。
 * ==========================================================================*/
(function (global, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) { module.exports = api; }
  else { global.GEO = api; }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var R = 6371008.8;                       // IUGG 平均地球半径（m）
  var rad = function (d) { return d * Math.PI / 180; };

  /* ---------------------------------------------------------------- 距离 */
  function haversine(a, b) {
    var p1 = rad(a.lat), p2 = rad(b.lat), dp = p2 - p1, dl = rad(b.lng - a.lng);
    var x = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(x)));
  }

  function pathLength(pts) {
    var s = 0;
    for (var i = 1; i < pts.length; i++) { s += haversine(pts[i - 1], pts[i]); }
    return s;
  }

  function segments(pts) {
    var out = [];
    for (var i = 1; i < pts.length; i++) { out.push(haversine(pts[i - 1], pts[i])); }
    return out;
  }

  /* 闭合误差：首点↔末点直线距离（环线应≈0）。 */
  function closureGap(pts) {
    if (!pts || pts.length < 2) { return null; }
    return haversine(pts[0], pts[pts.length - 1]);
  }

  function isClosed(pts, tol) {
    var g = closureGap(pts);
    return g !== null && g < (tol == null ? 30 : tol);
  }

  /* ------------------------------------------------------- 自交（打结）判定 */
  /* 平面近似（场地尺度足够）；只算"真正穿越"：共享端点的相邻段不算，
   * 只判别严格异号（共线/碰点不算自交）。**不做"隔几段"的额外跳过** ——
   * 否则小多边形（如蝴蝶结 4 点）的真实交叉会被吃掉。
   *
   * 性能：用」均匀网格粗筛」，而不是两两全比。网格边长取最长段长度 ⇒ 相交的
   * 两段必落在同一格 ⇒ 不漏报；复杂度从 O(n²) 降到近似 O(n)（n=3200 时
   * 34 ms -> <1 ms），否则手绘上干点时每次 mousemove 都掉帧。 */
  function _cross(p1, p2, p3, p4) {
    function o(a, b, c) { return Math.sign((b.lat - a.lat) * (c.lng - a.lng) - (b.lng - a.lng) * (c.lat - a.lat)); }
    var o1 = o(p1, p2, p3), o2 = o(p1, p2, p4), o3 = o(p3, p4, p1), o4 = o(p3, p4, p2);
    if (o1 === 0 || o2 === 0 || o3 === 0 || o4 === 0) { return false; }
    return o1 !== o2 && o3 !== o4;
  }

  function selfIntersections(pts, closed) {
    var n = pts.length;
    if (n < 4) { return []; }
    var segs = [];
    for (var i = 0; i < n - 1; i++) { segs.push([i, i + 1]); }
    if (closed) { segs.push([n - 1, 0]); }

    var maxSeg = 0, k = Math.cos(rad(pts[0].lat)) || 1;
    for (var s = 0; s < segs.length; s++) {
      var d = haversine(pts[segs[s][0]], pts[segs[s][1]]);
      if (d > maxSeg) { maxSeg = d; }
    }
    if (maxSeg <= 0) { return []; }
    var cell = maxSeg / 111195.0;            /* 网格边长（与 X/Y 同单位） */
    var X = function (p) { return p.lng * k; }, Y = function (p) { return p.lat; };

    var grid = new Map();
    function put(ix, iy, si) {
      var key = ix + ',' + iy;
      var arr = grid.get(key);
      if (!arr) { grid.set(key, arr = []); }
      arr.push(si);
    }
    for (var q = 0; q < segs.length; q++) {
      var a = pts[segs[q][0]], b = pts[segs[q][1]];
      var x0 = X(a), x1 = X(b), y0 = Y(a), y1 = Y(b);
      var ix0 = Math.floor(Math.min(x0, x1) / cell), ix1 = Math.floor(Math.max(x0, x1) / cell);
      var iy0 = Math.floor(Math.min(y0, y1) / cell), iy1 = Math.floor(Math.max(y0, y1) / cell);
      for (var ix = ix0; ix <= ix1; ix++) { for (var iy = iy0; iy <= iy1; iy++) { put(ix, iy, q); } }
    }

    var hits = [], seen = new Set();
    grid.forEach(function (list) {
      for (var m = 0; m < list.length; m++) {
        for (var o2 = m + 1; o2 < list.length; o2++) {
          var A = list[m], B = list[o2];
          var pk = A < B ? A + ':' + B : B + ':' + A;
          if (seen.has(pk)) { continue; }
          seen.add(pk);
          var i0 = segs[A][0], i1 = segs[A][1], j0 = segs[B][0], j1 = segs[B][1];
          if (i0 === j0 || i0 === j1 || i1 === j0 || i1 === j1) { continue; }
          if (_cross(pts[i0], pts[i1], pts[j0], pts[j1])) { hits.push([A, B]); }
        }
      }
    });
    return hits;
  }

  function selfIntersects(pts, closed) { return selfIntersections(pts, closed).length > 0; }

  /* ------------------------------------------------------------------ 平滑 */
  /* 滑动平均：去手抖，端点不动（开放路径）。这是默认"平滑"按钮用的算法。 */
  function smoothMovingAverage(pts, window, closed) {
    var n = pts.length;
    if (n < 3 || window < 3) { return pts.map(function (p) { return { lat: p.lat, lng: p.lng }; }); }
    var h = Math.floor(window / 2);
    var out = pts.map(function (p) { return { lat: p.lat, lng: p.lng }; });
    for (var i = 0; i < n; i++) {
      if (!closed && (i === 0 || i === n - 1)) { continue; }
      var la = 0, ln = 0, c = 0;
      for (var k = -h; k <= h; k++) {
        var j = i + k;
        if (closed) { j = ((j % n) + n) % n; }
        else if (j < 0 || j >= n) { continue; }
        la += pts[j].lat; ln += pts[j].lng; c++;
      }
      out[i] = { lat: la / c, lng: ln / c };
    }
    return out;
  }

  /* Chaikin 切角细分：更光滑、点数翻倍，但会"缩角"（密集点才可用，稀疏点别用）。 */
  function chaikin(pts, iters, closed) {
    var cur = pts.map(function (p) { return { lat: p.lat, lng: p.lng }; });
    iters = iters == null ? 1 : iters;
    for (var it = 0; it < iters; it++) {
      if (cur.length < 3) { break; }
      var next = [];
      if (!closed) { next.push({ lat: cur[0].lat, lng: cur[0].lng }); }
      var cnt = closed ? cur.length : cur.length - 1;
      for (var i = 0; i < cnt; i++) {
        var a = cur[i], b = cur[(i + 1) % cur.length];
        next.push({ lat: a.lat * 0.75 + b.lat * 0.25, lng: a.lng * 0.75 + b.lng * 0.25 });
        next.push({ lat: a.lat * 0.25 + b.lat * 0.75, lng: a.lng * 0.25 + b.lng * 0.75 });
      }
      if (!closed) { next.push({ lat: cur[cur.length - 1].lat, lng: cur[cur.length - 1].lng }); }
      cur = next;
    }
    return cur;
  }

  /* ---------------------------------------------------- 采样密度（按缩放自适应） */
  /* Web 墨卡托：某纬度每像素对应多少米。 */
  function metersPerPixel(lat, zoom) {
    return 156543.03392 * Math.cos(rad(lat)) / Math.pow(2, zoom);
  }

  /* 手绘采点间距：跟随缩放级别，放大就采得更密（保证放大后仍能画细）。 */
  function sampleSpacing(lat, zoom, min, max) {
    min = min == null ? 0.3 : min;
    max = max == null ? 5.0 : max;
    var m = metersPerPixel(lat, zoom);
    return Math.max(min, Math.min(max, m));
  }

  /* ------------------------------------------------------------------ 去重 */
  function dedupe(pts, minDist) {
    if (!pts || !pts.length) { return []; }
    var out = [{ lat: pts[0].lat, lng: pts[0].lng }];
    for (var i = 1; i < pts.length; i++) {
      if (haversine(out[out.length - 1], pts[i]) >= minDist) {
        out.push({ lat: pts[i].lat, lng: pts[i].lng });
      }
    }
    return out;
  }

  /* ------------------------------------------------- 闭合环补齐（供 repeat 用） */
  /* 尾点离首点 ≤ gapMax 就视为手动闭环，把首点补到末尾，避免最后一圈“缺一口”。 */
  function closeRing(pts, gapMax) {
    if (!pts || pts.length < 3) { return pts ? pts.slice() : []; }
    var g = haversine(pts[0], pts[pts.length - 1]);
    if (g > (gapMax == null ? 30 : gapMax) || g < 1e-6) { return pts.slice(); }
    return pts.concat([{ lat: pts[0].lat, lng: pts[0].lng }]);
  }

  /* --------------------------------------------------------- 点—段距离（平面近似） */
  /* 在目标点纬度上把经度缩放成“等长”，场地尺度下足够精确。 */
  function _ptSegDist(p, a, b) {
    var k = Math.cos(rad(p.lat));
    var ax = a.lng * k, ay = a.lat, bx = b.lng * k, by = b.lat, px = p.lng * k, py = p.lat;
    var dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
    var t = L2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2));
    var qx = ax + t * dx, qy = ay + t * dy;
    return haversine({ lat: py, lng: px / k }, { lat: qy, lng: qx / k });
  }

  /* Ramer–Douglas–Peucker：抽掉“弯不大的中间点”。epsM 单位米。 */
  function rdp(pts, epsM, closed) {
    var n = pts.length;
    if (n < 3) { return pts.map(function (p) { return { lat: p.lat, lng: p.lng }; }); }
    var keep = new Array(n).fill(false);
    keep[0] = keep[n - 1] = true;
    var stack = [[0, n - 1]];
    while (stack.length) {
      var ij = stack.pop(), i = ij[0], j = ij[1];
      var worst = -1, wi = -1;
      for (var k = i + 1; k < j; k++) {
        var d = _ptSegDist(pts[k], pts[i], pts[j]);
        if (d > worst) { worst = d; wi = k; }
      }
      if (worst > epsM && wi > i + 0) { keep[wi] = true; stack.push([i, wi], [wi, j]); }
    }
    var out = [];
    for (var t = 0; t < n; t++) { if (keep[t]) { out.push({ lat: pts[t].lat, lng: pts[t].lng }); } }
    return out;
  }

  /* ------------------------------------------------------ 等距重采样（沿折线走） */
  /* 按 stepM 均匀取点，形状不变、密度均匀 —— 更像真实 GPS 上报。
   * closed=true 时把首尾当作环，最后一圈走到起点为止。 */
  function resampleUniform(pts, stepM, closed) {
    if (!pts || pts.length < 2 || !(stepM > 0)) {
      return pts ? pts.map(function (p) { return { lat: p.lat, lng: p.lng }; }) : [];
    }
    var seq = closed ? pts.concat([pts[0]]) : pts;
    var out = [{ lat: seq[0].lat, lng: seq[0].lng }];
    var acc = 0;
    for (var i = 1; i < seq.length; i++) {
      var a = seq[i - 1], b = seq[i];
      var seg = haversine(a, b);
      if (seg === 0) { continue; }
      var t = 0;
      while (acc + (seg - t) >= stepM) {
        var need = stepM - acc;
        t += need;
        var f = t / seg;
        out.push({ lat: a.lat + (b.lat - a.lat) * f, lng: a.lng + (b.lng - a.lng) * f });
        acc = 0;
      }
      acc += seg - t;
    }
    var last = seq[seq.length - 1];
    var tail = out[out.length - 1];
    if (haversine(tail, last) > 1e-9) { out.push({ lat: last.lat, lng: last.lng }); }
    return out;
  }

  /* ------------------------------------------------------------ 路径整理总入口 */
  /* mode: 'raw'（只去重）/ 'smooth'（去抖）/ 'fit'（抽稀+等距重采样）。
   * 返回 {points, closed, mode, deleted}，不抛错。 */
  function fitPath(pts, opt) {
    opt = opt || {};
    var mode = opt.mode || 'raw';
    var dedupeM = opt.dedupeM == null ? 0.3 : opt.dedupeM;
    var closed = opt.closed != null ? opt.closed : isClosed(pts, opt.gapMax == null ? 30 : opt.gapMax);
    var stepM = opt.stepM == null ? 1.0 : opt.stepM;
    var win = opt.window == null ? 3 : opt.window;
    var epsM = opt.epsM == null ? 0.4 : opt.epsM;

    var out = dedupe(pts || [], dedupeM);
    if (mode === 'smooth') {
      out = smoothMovingAverage(out, win, closed);
      if (stepM > 0) { out = resampleUniform(out, stepM, closed); }
    } else if (mode === 'fit') {
      out = rdp(out, epsM, closed);
      if (stepM > 0) { out = resampleUniform(out, stepM, closed); }
    }
    if (closed) { out = closeRing(out, opt.gapMax == null ? 30 : opt.gapMax); }
    return { points: out, closed: closed, mode: mode, deleted: (pts ? pts.length : 0) - out.length };
  }

  /* 最长段下标（返回 i，表示 pts[i-1]→pts[i] 是最长段）；closed 时含收尾段。 */
  function longestSegmentIndex(pts, closed) {
    var n = pts.length;
    if (n < 2) { return -1; }
    var best = 1, bl = haversine(pts[0], pts[1]);
    for (var i = 2; i < n; i++) {
      var d = haversine(pts[i - 1], pts[i]);
      if (d > bl) { bl = d; best = i; }
    }
    if (closed) {
      var dc = haversine(pts[n - 1], pts[0]);
      if (dc > bl) { return n; }          // n 表示"收尾段"
    }
    return best;
  }

  /* -------------------------------------------------------- 折角/平滑程度度量 */
  /* 曲率能量：相邻三点中点到中点的偏移平方和。手抖越大值越大（用于自测）。 */
  function roughness(pts, closed) {
    var n = pts.length;
    if (n < 3) { return 0; }
    var s = 0, i = closed ? 0 : 1, end = closed ? n : n - 1;
    for (; i < end; i++) {
      var a = pts[(i - 1 + n) % n], b = pts[i], c = pts[(i + 1) % n];
      var ml = (a.lat + c.lat) / 2, mn = (a.lng + c.lng) / 2;
      var dl = b.lat - ml, dn = b.lng - mn;
      s += dl * dl + dn * dn;
    }
    return s;
  }

  /* ================================================================ 标准跑道 */
  /* IAAF 400 m 跑道：直道 84.39 m，第 1 道测量半径 36.8 m（=36.5 内沿 + 0.30 测量线），
   * 道宽 1.22 m。第 n 道单圈 = 2×84.39 + 2π(36.8 + (n-1)×1.22)。
   * 第 1 道 = 400.00 m（与田径场标定一致）。 */
  var TRACK = { straights: 84.39, lane1Radius: 36.8, laneWidth: 1.22 };

  function laneRadius(lane) { return TRACK.lane1Radius + (Math.max(1, lane) - 1) * TRACK.laneWidth; }
  function trackLapLength(lane) { return 2 * TRACK.straights + 2 * Math.PI * laneRadius(lane); }
  function trackLapCount(lane, targetM) { return targetM / trackLapLength(lane); }

  /* 生成一圈闭合跑道（体育场形）。
   * o = {lat,lng, lane=1, headingDeg=0（直道前进方向的罗盘角，0=北）, ccw=false,
   *      spacingM=2.0, phaseDeg=0（沿跑道错开起点的相位）}
   * 返回按顺时针/逆时针行走的 {lat,lng}[]，末点与首点重合（闭合）。 */
  function trackLoop(o) {
    o = o || {};
    var lat0 = o.lat, lng0 = o.lng;
    var r = laneRadius(o.lane == null ? 1 : o.lane);
    var s = o.straights == null ? TRACK.straights : o.straights;
    var stepM = o.spacingM == null ? 2.0 : o.spacingM;
    var th = rad(o.headingDeg == null ? 0 : o.headingDeg);
    var sgn = o.ccw ? -1 : 1;
    var a = s / 2, P = 2 * s + 2 * Math.PI * r;

    /* t 是沿跑道的里程（m）；返回局部坐标 [前进方向, 左向] */
    function at(t) {
      t = ((t % P) + P) % P;
      if (t < s) { return [-a + t, r]; }
      if (t < s + Math.PI * r) {
        var g = Math.PI / 2 - (t - s) / r;
        return [a + r * Math.cos(g), r * Math.sin(g)];
      }
      if (t < 2 * s + Math.PI * r) { return [a - (t - s - Math.PI * r), -r]; }
      var g2 = -Math.PI / 2 - (t - 2 * s - Math.PI * r) / r;
      return [-a + r * Math.cos(g2), r * Math.sin(g2)];
    }

    var n = Math.max(16, Math.round(P / stepM));
    var out = [];
    var t0 = (o.phaseDeg || 0) / 360 * P;
    var cosT = Math.cos(th), sinT = Math.sin(th), cosLat = Math.cos(rad(lat0));
    for (var i = 0; i <= n; i++) {
      var t = t0 + sgn * i * (P / n);
      var c = at(t), u = c[0], v = c[1];
      /* (前进,左) -> (东,北)：航向 th 的前方=(sinθ,cosθ)，左方=(−cosθ,sinθ) */
      var east = u * sinT - v * cosT, north = u * cosT + v * sinT;
      out.push({ lat: lat0 + north / 111195.0, lng: lng0 + east / (111195.0 * cosLat) });
    }
    return out;                       // i 从 0..n，已含闭合点
  }

  /* 平面 signed area（m²）：用于判方向（正=逆期，负=顺时针）。 */
  function signedArea(pts) {
    if (!pts || pts.length < 3) { return 0; }
    var lat0 = pts[0].lat, k = 111195.0, kx = k * Math.cos(rad(lat0));
    var A = 0;
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i], q = pts[(i + 1) % pts.length];
      var x1 = (p.lng - pts[0].lng) * kx, y1 = (p.lat - lat0) * k;
      var x2 = (q.lng - pts[0].lng) * kx, y2 = (q.lat - lat0) * k;
      A += x1 * y2 - x2 * y1;
    }
    return A / 2;
  }

  /* ============================================================ 坐标变换 GCJ-02 */
  /* 与 Python 端 `tracklib.py` 的 `wgs84_to_gcj02()` **同一套算法与同一组常量**
   * （`_A`/`_EE`/`_tf_lat`/`_tf_lng`/边界判定逐行对应），所以「画的位置」与
   * 「导出给 app 的位置」是同一个函数算出来的，不会各自偏一点。
   * 正向必须一致；反向（gcj02ToWgs84）用迭代精确反解（仅用于在 GCJ 底图上读点）。 */
  var _A = 6378245.0, _EE = 0.00669342162296594323;
  function outOfChina(p) {
    return !(p.lng > 72.004 && p.lng < 137.8347 && p.lat > 0.8293 && p.lat < 55.8271);
  }
  function _tfLat(x, y) {
    var r = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x)));
    r += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
    r += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0;
    return r;
  }
  function _tfLng(x, y) {
    var r = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x)));
    r += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)) * 2.0 / 3.0;
    r += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0;
    return r;
  }
  function wgs84ToGcj02(p) {
    if (outOfChina(p)) { return { lat: p.lat, lng: p.lng }; }
    var dLat = _tfLat(p.lng - 105.0, p.lat - 35.0), dLng = _tfLng(p.lng - 105.0, p.lat - 35.0);
    var rlat = rad(p.lat), m = Math.sin(rlat);
    m = 1 - _EE * m * m;
    var sm = Math.sqrt(m);
    dLat = dLat * 180.0 / ((_A * (1 - _EE)) / (m * sm) * Math.PI);
    dLng = dLng * 180.0 / (_A / sm * Math.cos(rlat) * Math.PI);
    return { lat: p.lat + dLat, lng: p.lng + dLng };
  }
  /* 迭代反解（比对 Python 端的一步近似更准；只在 GCJ 底图上读点时用） */
  function gcj02ToWgs84(p) {
    if (outOfChina(p)) { return { lat: p.lat, lng: p.lng }; }
    var cur = { lat: p.lat, lng: p.lng };
    for (var i = 0; i < 10; i++) {
      var f = wgs84ToGcj02(cur);
      var dLat = f.lat - p.lat, dLng = f.lng - p.lng;
      if (Math.abs(dLat) < 1e-12 && Math.abs(dLng) < 1e-12) { break; }
      cur = { lat: cur.lat - dLat, lng: cur.lng - dLng };
    }
    return cur;
  }

  return {
    R: R,
    TRACK: TRACK,
    outOfChina: outOfChina,
    wgs84ToGcj02: wgs84ToGcj02,
    gcj02ToWgs84: gcj02ToWgs84,
    laneRadius: laneRadius,
    trackLapLength: trackLapLength,
    trackLapCount: trackLapCount,
    trackLoop: trackLoop,
    signedArea: signedArea,
    haversine: haversine,
    pathLength: pathLength,
    segments: segments,
    closureGap: closureGap,
    isClosed: isClosed,
    selfIntersections: selfIntersections,
    selfIntersects: selfIntersects,
    smoothMovingAverage: smoothMovingAverage,
    chaikin: chaikin,
    metersPerPixel: metersPerPixel,
    sampleSpacing: sampleSpacing,
    dedupe: dedupe,
    closeRing: closeRing,
    rdp: rdp,
    resampleUniform: resampleUniform,
    fitPath: fitPath,
    longestSegmentIndex: longestSegmentIndex,
    roughness: roughness
  };
});
