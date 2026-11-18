"""剪映蒙版 → 达芬奇 Fusion comp 实写（Lua 通道）。

已实测可用（Fusion 21.1）：
  - comp:AddTool('PolylineMask') 可建节点，自动带子样条 Polygon1多边形折线(BezierSpline)
  - 点位写入必须走子样条的 SetKeyFrames（直接赋 t.Polyline 无效；t.Polyline 是 userdata 无此方法）
  - Polyline 值必须用 bmd.readstring("Polyline { Points = { ... } }") 构造，纯 table 赋值会崩
  - MediaIn.EffectMask = tool 可连通
  - 中文节点名可用，但更稳的是按 RegID 找 BezierSpline

坐标：剪映 contour 蒙版锚点中心原点 ±1、**y 向上**(与 Fusion 同向, 实测勘误:
取负会上下颠倒, 用户 2026-11-17 确认) → Fusion 路径空间(中心原点, ±0.5):
      fx=x/2, fy=y/2；切线偏移量 LX=lcx/2, LY=lcy/2 (同样不取负)
      (注意: RectangleMask 的 Width/Height 与 Text+ 的 Center 仍用 0..1 空间, 勿混用)
"""

from __future__ import annotations

# 剪映 feather → Fusion SoftEdge 换算比例。
# 用户目测标定(2026-11-17): 达芬奇 SoftEdge 0.1 ≈ 剪映羽化滑杆 100
# → SoftEdge = 剪映feather(0-1) × 0.1 (如 0.84 → 0.084)
# 原始值会写进节点 CustomData 的 NLEBridge.SourceFeather, 改此系数重跑即可重放。
# TODO: 如需更精确, 用同帧对比修正此系数
FEATHER_SCALE = 0.1


def apply_feather(jy_feather: float) -> float:
    """剪映 feather → Fusion SoftEdge。未标定前返回 0。"""
    try:
        f = float(jy_feather or 0)
    except (TypeError, ValueError):
        f = 0.0
    return f * FEATHER_SCALE


def jy_to_fusion(x: float, y: float) -> tuple[float, float]:
    """剪映锚点(中心原点±1, y向上) → Fusion 路径空间(中心原点, ±0.5, y向上)。"""
    return x / 2.0, y / 2.0


def _n(v: float) -> str:
    s = "%.6f" % float(v)
    s = s.rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def points_to_polyline_str(points: list[dict]) -> str:
    """剪映 contour → Fusion Polyline 值字符串（保留贝塞尔切线）。"""
    has_ctrl = any(
        abs(p.get("left_control", {}).get("x", 0)) > 1e-9
        or abs(p.get("left_control", {}).get("y", 0)) > 1e-9
        or abs(p.get("right_control", {}).get("x", 0)) > 1e-9
        or abs(p.get("right_control", {}).get("y", 0)) > 1e-9
        for p in points
    )
    rows = []
    for p in points:
        ax = p["anchor"]["x"]
        ay = p["anchor"]["y"]
        fx, fy = jy_to_fusion(ax, ay)
        if has_ctrl:
            lc = p.get("left_control") or {}
            rc = p.get("right_control") or {}
            lx, ly = float(lc.get("x", 0)) / 2.0, float(lc.get("y", 0)) / 2.0
            rx, ry = float(rc.get("x", 0)) / 2.0, float(rc.get("y", 0)) / 2.0
            rows.append("{ X = %s, Y = %s, Linear = false, LockY = true, "
                        "LX = %s, LY = %s, RX = %s, RY = %s }"
                        % (_n(fx), _n(fy), _n(lx), _n(ly), _n(rx), _n(ry)))
        else:
            rows.append("{ X = %s, Y = %s, Linear = true, LockY = true }" % (_n(fx), _n(fy)))
    # 重复首点只会得到开放路径，必须声明 Closed 才能生成实体遮罩。
    return "Polyline { Closed = true, Points = {\n\t\t\t\t\t\t\t" + ",\n\t\t\t\t\t\t\t".join(rows) + "\n\t\t\t\t\t\t} }"


LUA_WIRE_MASK = r"""
-- 统一接线: MediaIn(EffectMask=蒙版) -> MediaOut
-- 蒙版接在生产者节点(Loader/MediaIn)上是 Fusion 标准抠 alpha 用法:
-- 蒙版外 alpha=0, 编辑页自动透下层; 羽化软边保留
local mask = comp:FindTool("NBPolyMask") or comp:FindTool("NBRectMask")
if not mask then RES = "NO MASK" return end
local mi = comp:FindTool("MediaIn1") or comp:FindTool("MediaIn")
local mo = comp:FindTool("MediaOut1")
if not mi or not mo then RES = "NO MI/MO" return end
-- 旧的 CB 中转已无用, 删除
local cb = comp:FindTool("NBChan")
if cb then pcall(function() cb:Delete() end) end
mo.Input = mi
local ok = pcall(function() mi:SetInput("EffectMask", mask) end)
pcall(function() mi.EffectMask = mask end)
RES = "wired " .. mask:GetAttrs().TOOLS_Name .. " set=" .. tostring(ok)
"""

LUA_WRITE_POLY = r"""
local OK, ERR = true, ""
local t = comp:FindTool("NBPolyMask")
if not t then
  OK, ERR = pcall(function()
    t = comp:AddTool('PolylineMask')
    t:SetAttrs({TOOLS_Name = "NBPolyMask"})
  end)
  if not OK then RES = "addtool fail: " .. tostring(ERR) end
end
if t then
  -- 找子样条(BezierSpline)
  local sp = nil
  for _, tool in pairs(comp:GetToolList()) do
    if tool:GetAttrs().TOOLS_RegID == "BezierSpline" then
      sp = tool
      break
    end
  end
  if not sp then
    RES = "spline not found"
  else
    local poly = bmd.readstring([[%(poly)s]])
    local ok2, err2 = pcall(function()
      sp:SetKeyFrames({ [0] = { 0, Flags = { Linear = true, LockedY = true }, Value = poly } })
    end)
    -- 基础参数 + 保留剪映原始羽化(供标定后重放)
    pcall(function()
      t.Solid = 1.0
      t.Level = 1.0
      t.Invert = %(invert)d
      t.SoftEdge = %(feather)s
      t.Filter = "Fast Gaussian"
      t:SetData("NLEBridge.SourceFeather", %(srcfeather)s)
    end)
    RES = "poly=" .. tostring(ok2) .. " err=" .. tostring(err2)
  end
end
"""

LUA_WRITE_RECT = r"""
local OK, ERR = true, ""
local t = comp:FindTool("NBRectMask")
if not t then
  OK, ERR = pcall(function()
    t = comp:AddTool('RectangleMask')
    t:SetAttrs({TOOLS_Name = "NBRectMask"})
  end)
end
if t then
  pcall(function()
    t.Width = %(w)s
    t.Height = %(h)s
    t.Center = { 0.5, 0.5 }
    t.Solid = 1.0
    t.Level = 1.0
    t.Invert = %(invert)d
    t.SoftEdge = %(feather)s
    t.Filter = "Fast Gaussian"
    t:SetData("NLEBridge.SourceFeather", %(srcfeather)s)
  end)
  RES = "rect ok=" .. tostring(OK)
end
"""


def poly_lua(points: list[dict], feather: float, invert: int = 0) -> str:
    return LUA_WRITE_POLY % {
        "poly": points_to_polyline_str(points),
        "feather": _n(apply_feather(feather)),
        "invert": invert,
        "srcfeather": _n(float(feather or 0)),
    }


def wire_lua() -> str:
    return LUA_WIRE_MASK


def rect_lua(width: float, height: float, feather: float, invert: int = 0) -> str:
    return LUA_WRITE_RECT % {
        "w": _n(width), "h": _n(height),
        "feather": _n(apply_feather(feather)), "invert": invert,
        "srcfeather": _n(float(feather or 0)),
    }
