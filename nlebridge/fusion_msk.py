"""把剪映蒙版写入达芬奇片段的 Fusion comp（保持可编辑性）。

设计原则:
  - 匹配剪映效果优先: 形状蒙版 → Polygon(PolylineMask) 节点, 点位/羽化可直接在
    Fusion 里拖改; AI 抠像 → Loader(序列帧) + BitmapMask 亮度作遮罩, 换帧/删改
    直接动 out/masks/ 下的 PNG 即可。
  - 一律通过 EffectMask 连接, 蒙版外区域 alpha=0, 时间线上自动透出下层轨。
  - 坐标换算: 剪映蒙版坐标为中心原点、±1 归一化、y 向下;
    Fusion 为左下原点 0..1、y 向上:  fx=(x+1)/2, fy=(1-y)/2。

Lua 注意: comp.Execute 内工具操作实际生效, 但工具对象相关语句之后的
SetData 可能中断 —— 因此执行后一律用 Python 端 GetInput 读回验证。
"""

from __future__ import annotations


def jy_to_fusion(x: float, y: float) -> tuple[float, float]:
    """剪映蒙版坐标 → Fusion 归一化坐标。"""
    return (x + 1.0) / 2.0, (1.0 - y) / 2.0


def lua_num(v: float) -> str:
    return ("%.6f" % float(v)).rstrip("0").rstrip(".")


def polyline_points_lua(points: list[dict]) -> str:
    """剪映 contour 锚点 → Fusion Polyline 值 (直线段)。"""
    rows = []
    for p in points:
        fx, fy = jy_to_fusion(p["anchor"]["x"], p["anchor"]["y"])
        rows.append("{ Linear = true, LockY = true, X = %s, Y = %s }"
                    % (lua_num(fx), lua_num(fy)))
    return "Polyline { Closed = true, Points = {\n" + ",\n".join(rows) + "\n} }"


def shape_mask_lua(points: list[dict], feather: float | None,
                   tool_name: str = "NBPolyMask") -> str:
    """创建形状蒙版并连到 MediaIn 的 EffectMask。"""
    lua = f"""
local t = comp:AddTool('PolylineMask')
t:SetAttrs({{TOOLS_Name = '{tool_name}'}})
t.Polyline = {polyline_points_lua(points)}
t.Solid = 1.0
t.Level = 1.0
t.Invert = 0
t.SoftEdge = {lua_num(feather if feather is not None else 0.0)}
local mi
for _, tool in pairs(comp:GetToolList()) do
    if tool:GetAttrs().TOOLS_RegID == 'MediaIn' then mi = tool break end
end
if mi then mi.EffectMask = t end
"""
    return lua


def rect_mask_lua(width: float, height: float, feather: float | None,
                  tool_name: str = "NBRectMask") -> str:
    """矩形蒙版 (剪映 config.width/height 为宽高占比, 居中)。"""
    return f"""
local t = comp:AddTool('RectangleMask')
t:SetAttrs({{TOOLS_Name = '{tool_name}'}})
t.Width = {lua_num(width)}
t.Height = {lua_num(height)}
t.Solid = 1.0
t.Level = 1.0
t.Invert = 0
t.SoftEdge = {lua_num(feather if feather is not None else 0.0)}
local mi
for _, tool in pairs(comp:GetToolList()) do
    if tool:GetAttrs().TOOLS_RegID == 'MediaIn' then mi = tool break end
end
if mi then mi.EffectMask = t end
"""


def alpha_matte_lua(seq_path: str, frame_count: int, scale: float,
                    tool_name: str = "NBAlphaMatte") -> str:
    """AI 抠像: Loader 加载 alpha PNG 序列 → Transform 放大 → BitmapMask(亮度)
    → MediaIn.EffectMask。seq_path 用 frame_0001.png (Fusion 自动识别序列)。"""
    win = seq_path.replace("\\", "/")
    return f"""
local ld = comp:AddTool('Loader')
ld:SetAttrs({{TOOLS_Name = '{tool_name}Loader'}})
ld.Clip = '{win}'
ld.GlobalStart = 0
ld.GlobalEnd = {int(frame_count) - 1}
ld.Loop = 0
ld.HoldFirstFrame = 0
ld.HoldLastFrame = {int(frame_count) - 1}
local tr = comp:AddTool('Transform')
tr:SetAttrs({{TOOLS_Name = '{tool_name}Scale'}})
tr.Size = {lua_num(scale)}
tr.Center = {{0.5, 0.5}}
ld.Out := tr.TransformIn
local bm = comp:AddTool('BitmapMask')
bm:SetAttrs({{TOOLS_Name = '{tool_name}'}})
-- ⚠️ BitmapMask 的图像输入端口叫 Image, 不是 Input;
-- 写成 bm.Input / SetInput('Input', ...) 会静默失败, 空 mask = 整段被抠黑。
bm.Image = tr
bm.Channel = 'Alpha'
bm.Level = 1.0
bm.SoftEdge = 0.0
local mi
for _, tool in pairs(comp:GetToolList()) do
    if tool:GetAttrs().TOOLS_RegID == 'MediaIn' then mi = tool break end
end
if mi then mi.EffectMask = bm end
"""
