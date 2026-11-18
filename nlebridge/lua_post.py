# -*- coding: utf-8 -*-
"""达芬奇后处理 Lua 脚本生成器

背景: fusionscript.dll 绑定达芬奇自带的旧版 CPython, 本机 Python 3.13 加载不了,
无法用 Python 脚本直连达芬奇; 但达芬奇控制台(Workspace > Console)原生支持 Lua,
所以淡变这类"FCPXML 导入带不过来"的属性, 用生成的 Lua 脚本在导入后补。

设计要点(踩过的坑):
  - 绝不静默: 全流程 pcall; 结果写 %TEMP%/nlebridge_fades.log 并自动用记事本打开。
    控制台不开也能看到结果 —— 之前只 print, 用户看到的就是"点了没反应"。
  - resolve 句柄兼容三种环境:
      脚本菜单 / 控制台 Lua   -> 全局 resolve (userdata)
      Fusion 页面            -> 需要调用 Resolve() 才拿得到
    两种都试, 都失败就把诊断写进日志。
  - SetProperty 的淡变键名不保证叫 FadeIn, 逐个候选试并读回验证。
  - 匹配不上时给出"最近片段"对照, 便于修正帧对齐。
"""
import os

# 淡变属性候选键名, 按可能性排序
FADE_IN_KEYS = ("FadeIn", "Fade In", "FadeInDuration")
FADE_OUT_KEYS = ("FadeOut", "Fade Out", "FadeOutDuration")

_LUA_TMPL = r"""-- __NAME__
-- nle-bridge 自动生成: 达芬奇导入 FCPXML 后, 补剪映的音频/视频淡入淡出。
--
-- 跑法 A(推荐): 打开目标时间线 -> 菜单 工作区 > 脚本 > Utility > __STUB__
-- 跑法 B: 工作区 > 控制台(语言切 Lua) -> 粘贴本文件全部内容 -> 回车
--
-- 结果不依赖控制台: 会在 %TEMP%\nlebridge_fades.log 记录全过程,
-- 跑完自动用记事本打开该日志。

local LOG = {}
local LOGPATH = ""

local function log(s)
    LOG[#LOG + 1] = tostring(s)
    pcall(print, s)
end

local function resolve_logpath()
    local tmp = os.getenv("TEMP") or os.getenv("TMP") or "C:/Windows/Temp"
    tmp = tmp:gsub("\\", "/")
    if tmp:sub(-1) == "/" then tmp = tmp:sub(1, -2) end
    return tmp .. "/nlebridge_fades.log"
end

local function flush_log()
    local f = io.open(LOGPATH, "wb")
    if not f then return false end
    -- UTF-8 BOM: 记事本才认得出中文
    f:write("\239\187\191")
    f:write(table.concat(LOG, "\r\n"))
    f:close()
    return true
end

local function open_log()
    local win = LOGPATH:gsub("/", "\\")
    pcall(os.execute, 'start "" notepad "' .. win .. '"')
end

local FPS = __FPS__
local TARGETS = {
__TARGETS__
}

local FADE_IN_KEYS = { __FI_KEYS__ }
local FADE_OUT_KEYS = { __FO_KEYS__ }

local function tc(frame)
    frame = math.floor(frame + 0.5)
    local f = frame % FPS
    local s = math.floor(frame / FPS)
    return string.format("%02d:%02d:%02d:%02d",
        math.floor(s / 3600), math.floor(s / 60) % 60, s % 60, f)
end

local function has_pm(g)
    if g == nil then return false end
    local t = type(g)
    if t ~= "userdata" and t ~= "table" then return false end
    local ok, v = pcall(function() return g.GetProjectManager end)
    return ok and v ~= nil
end

local function get_resolve()
    local ok, g = pcall(function() return resolve end)
    if ok and has_pm(g) then return g, "全局 resolve" end

    local ok2, fn = pcall(function() return Resolve end)
    if ok2 and type(fn) == "function" then
        local ok3, v = pcall(fn)
        if ok3 and has_pm(v) then return v, "Resolve()" end
    end

    -- Fusion 环境(脚本菜单/ Fusion 页面)的老路子
    local okb, b = pcall(function() return bmd end)
    if okb and b ~= nil then
        local ok4, v4 = pcall(function() return b.scriptapp("Resolve") end)
        if ok4 and has_pm(v4) then return v4, 'bmd.scriptapp("Resolve")' end
    end
    return nil, nil
end

local function try_set(item, keys, frames)
    for _, k in ipairs(keys) do
        local ok, res = pcall(function() return item:SetProperty(k, frames) end)
        if ok and res then
            local ok2, val = pcall(function() return item:GetProperty(k) end)
            return true, k, (ok2 and val or "?")
        end
    end
    return false, nil, nil
end

-- Resolve 21.1+: SetFades({FadeIn=帧, FadeOut=帧}) 是正路;
-- 读回现有值合并, 避免覆盖另一侧已写入的淡变。
local function set_fades(item, fi, fo)
    local cur = {}
    local okG, got = pcall(function() return item:GetFades() end)
    if okG and type(got) == "table" then cur = got end
    local want = {
        FadeIn = (fi > 0) and fi or (tonumber(cur.FadeIn) or 0),
        FadeOut = (fo > 0) and fo or (tonumber(cur.FadeOut) or 0),
    }
    local okS, res = pcall(function() return item:SetFades(want) end)
    if not (okS and res) then return false, nil end
    local okV, back = pcall(function() return item:GetFades() end)
    if okV and type(back) == "table" then
        local gi = tonumber(back.FadeIn) or 0
        local go = tonumber(back.FadeOut) or 0
        if gi == want.FadeIn and go == want.FadeOut then
            return true, string.format("SetFades in=%d out=%d", gi, go)
        end
        return false, string.format("读回不符 in=%d/%d out=%d/%d", gi, want.FadeIn, go, want.FadeOut)
    end
    return true, "SetFades(读回不可用)"
end

local function main()
    log("======== nle-bridge 淡变后处理 ========")
    log("时间: " .. tostring(os.date and os.date("%Y-%m-%d %H:%M:%S") or "?"))
    log("Lua: " .. tostring(_VERSION))

    local r, how = get_resolve()
    if not r then
        log("[X] 拿不到 Resolve 对象。")
        log("    说明这段代码没有被达芬奇的 Lua 环境执行(或运行在 Fusion 页面里)。")
        log("    请改用: 工作区 > 控制台, 左下语言下拉选 Lua, 粘贴整段后回车。")
        return
    end
    log("Resolve 句柄: " .. how)

    local okV, ver = pcall(function() return r:GetVersionString() end)
    log("达芬奇版本: " .. (okV and tostring(ver) or "?"))

    local pm = r:GetProjectManager()
    if not pm then log("[X] 没有 ProjectManager(工程管理器未就绪)"); return end
    local proj = pm:GetCurrentProject()
    if not proj then log("[X] 没有打开的工程"); return end
    log("工程: " .. tostring(proj:GetName()))

    local tl = proj:GetCurrentTimeline()
    if not tl then
        log("[X] 当前没有打开的时间线 —— 先在媒体池双击那条时间线再跑本脚本")
        return
    end
    log("时间线: " .. tostring(tl:GetName()))

    local base = tl:GetStartFrame() or 0
    local okS, fpsS = pcall(function() return tl:GetSetting("timelineFrameRate") end)
    log(string.format("起始帧 %d, 时间线帧率 %s (脚本按 %d 帧算)", base,
        okS and tostring(fpsS) or "?", FPS))

    -- 收集所有片段
    local items = {}
    for _, kind in ipairs({ "video", "audio" }) do
        local okC, n = pcall(function() return tl:GetTrackCount(kind) end)
        n = (okC and tonumber(n)) or 0
        local cnt = 0
        for i = 1, n do
            local okL, list = pcall(function() return tl:GetItemListInTrack(kind, i) end)
            for _, it in ipairs((okL and list) or {}) do
                items[#items + 1] = { item = it, kind = kind, track = i, start = it:GetStart() }
                cnt = cnt + 1
            end
        end
        log(string.format("  %s 轨 %d 条, 片段 %d 个", kind, n, cnt))
    end
    log("目标淡变: " .. #TARGETS .. " 处")
    log("")

    if #items == 0 then
        log("[X] 时间线上一个片段都没有, 检查是否导入成功")
        return
    end

    local applied, failed = {}, {}
    for _, t in ipairs(TARGETS) do
        local want = base + t[1]
        local wantKind = t[3] and "audio" or "video"
        local hit = nil
        for _, e in ipairs(items) do
            if e.kind == wantKind and e.start == want then hit = e.item break end
        end

        if not hit then
            -- 找同类轨最近的片段, 方便判断是不是整体偏了几帧
            local nearest, nd = nil, nil
            for _, e in ipairs(items) do
                if e.kind == wantKind then
                    local d = math.abs(e.start - want)
                    if nd == nil or d < nd then nearest, nd = e, d end
                end
            end
            local hint = ""
            if nearest and nd and nd <= FPS * 2 then
                hint = string.format(" (最近片段相差 %d 帧: %s)", nearest.start - want, tc(nearest.start))
            end
            failed[#failed + 1] = string.format("[X] %s @ 目标 %s: 时间线上没找到%s片段%s",
                t[2], tc(want), wantKind, hint)
        else
            -- 21.1+: 先走 SetFades; 失败再退回旧键名逐个试
            local okF, detail = set_fades(hit, t[4], t[5])
            if okF then
                applied[#applied + 1] = string.format(
                    "[OK] %s @ %s (%s): 淡入 %d 帧 / 淡出 %d 帧  (%s)",
                    t[2], tc(want), wantKind, t[4], t[5], tostring(detail))
            else
                local sIn, kIn, vIn = true, nil, nil
                local sOut, kOut, vOut = true, nil, nil
                if t[4] > 0 then sIn, kIn, vIn = try_set(hit, FADE_IN_KEYS, t[4]) end
                if t[5] > 0 then sOut, kOut, vOut = try_set(hit, FADE_OUT_KEYS, t[5]) end
                if sIn and sOut then
                    applied[#applied + 1] = string.format(
                        "[OK] %s @ %s (%s): 淡入 %d 帧 / 淡出 %d 帧  读回 %s=%s %s=%s",
                        t[2], tc(want), wantKind, t[4], t[5],
                        tostring(kIn or "-"), tostring(vIn or "-"),
                        tostring(kOut or "-"), tostring(vOut or "-"))
                else
                    failed[#failed + 1] = string.format(
                        "[X] %s @ %s (%s): API 写淡变失败(%s) -> "
                        .. "手动补 淡入 %d 帧 / 淡出 %d 帧",
                        t[2], tc(want), wantKind, tostring(detail or "SetProperty 全部键名失败"),
                        t[4], t[5])
                end
            end
        end
    end

    log("---------- 成功 " .. #applied .. " / " .. #TARGETS .. " ----------")
    for _, m in ipairs(applied) do log(m) end
    if #failed > 0 then
        log("---------- 需处理 " .. #failed .. " ----------")
        for _, m in ipairs(failed) do log(m) end
    end

    -- 全部片段清单: 和剪映逐条对账, 定位"片段丢失/位置错乱"; 附启用状态
    log("")
    log("---------- 全部片段清单(与剪映对账) ----------")
    local nDisabled = 0
    for _, e in ipairs(items) do
        local nm, en, fp, en2 = "?", e.start, "?", true
        pcall(function() nm = e.item:GetName() end)
        pcall(function() en = e.item:GetEnd() end)
        pcall(function()
            local mpi = e.item:GetMediaPoolItem()
            if mpi then fp = mpi:GetClipProperty("File Path") or "?" end
        end)
        local okE, ev = pcall(function() return e.item:GetClipEnabled() end)
        if okE and ev == false then
            en2 = false
            nDisabled = nDisabled + 1
        end
        log(string.format("%s轨%d 起%d 止%d  %s  [%s]%s",
            e.kind, e.track, e.start, en, tostring(nm), tostring(fp),
            en2 and "" or "  <<已停用"))
    end
    if nDisabled > 0 then
        log(string.format(">> 时间线上有 %d 个片段被停用(灰显) —— 播放/导出时不发声不发画面", nDisabled))
    end

    -- 探测本版 API 到底有哪些片段属性(找 fade 相关键)
    log("")
    log("---------- 片段属性探测(GetProperties) ----------")
    local probe = nil
    for _, e in ipairs(items) do
        if e.kind == "audio" then probe = e.item break end
    end
    if probe then
        local okP, props = pcall(function() return probe:GetProperties() end)
        if okP and type(props) == "table" then
            local keys, fadeKeys = {}, {}
            for k in pairs(props) do
                keys[#keys + 1] = tostring(k)
                if tostring(k):lower():find("fade") then fadeKeys[#fadeKeys + 1] = tostring(k) end
            end
            table.sort(keys)
            log("属性键: " .. table.concat(keys, ", "))
            if #fadeKeys > 0 then
                log(">> 发现 fade 相关键: " .. table.concat(fadeKeys, ", "))
            else
                log("(没有任何 fade 键 —— 21.0 的 API 确实不支持片段淡变)")
            end
        else
            log("GetProperties 不可用: " .. tostring(okP) .. " / " .. tostring(props))
        end
    else
        log("没有音频片段可探测")
    end

    if #applied == 0 and #items > 0 then
        log("")
        log("匹配全失败时的排查: 下面列出同类片段的实际起始帧, 对比上面的目标帧即可判断偏移")
        local shown = 0
        for _, e in ipairs(items) do
            if shown >= 15 then break end
            log(string.format("  %s 起于 帧%d (%s)  %s", e.kind, e.start, tc(e.start),
                tostring(e.item:GetName())))
            shown = shown + 1
        end
    end
    log("======================================")
end

local ok, err = pcall(main)
LOGPATH = resolve_logpath()
if not ok then
    LOG[#LOG + 1] = "[X] 脚本异常: " .. tostring(err)
    pcall(print, "[X] 脚本异常: " .. tostring(err))
end
if #LOG == 0 then
    LOG[#LOG + 1] = "[X] 脚本没有产生任何输出, 可能被环境拦截"
end
flush_log()
open_log()
"""


def _frames(us: int, fps: int) -> int:
    return int(round(us * fps / 1_000_000))


def _lua_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _lua_keys(keys) -> str:
    return ", ".join('"%s"' % k for k in keys)


def fade_entries(tl):
    """收集所有有淡变的片段 -> [{start_f, name, is_audio, fi_f, fo_f, pos_s}]

    音频片段: 声音淡变 (c.fade_in/fade_out)
    视频片段: 画面叠化(渐显/渐隐, c.vfade_*) 优先; 没有自带声音的片段
              (图片/无音频流) 其声音淡变也落在视频片段上 —— 两者取并。
    """
    out = []
    fps = tl.fps
    for tr in tl.tracks:
        if tr.kind not in ("video", "audio"):
            continue
        for c in tr.clips:
            if tr.kind == "audio":
                fi_us, fo_us = c.fade_in, c.fade_out
            else:
                # ⚠️ 只认画面动画(渐显/渐隐)。视频片段自身的 fade_* 是声音淡化,
                # track 模式下声音已分离到音轨, 片段上残留的是死数据, 写到画面
                # 上会让达芬奇凭空出淡化手柄(2026-11-16 实测)。
                fi_us = getattr(c, "vfade_in", 0)
                fo_us = getattr(c, "vfade_out", 0)
            if not (fi_us or fo_us):
                continue
            m = tl.media.get(c.media_id)
            name = (m.name if m else "") or ""
            out.append({
                "start_f": _frames(c.start, fps),
                "name": name,
                "is_audio": tr.kind == "audio",
                "fi_f": _frames(fi_us, fps),
                "fo_f": _frames(fo_us, fps),
                "pos_s": c.start / 1e6,
            })
    out.sort(key=lambda e: e["start_f"])
    return out


def generate(tl, out_path: str) -> str:
    fps = tl.fps
    entries = fade_entries(tl)

    lines = []
    for e in entries:
        lines.append('    { %d, "%s", %s, %d, %d },  -- %s'
                     % (e["start_f"], _lua_str(e["name"]),
                        "true" if e["is_audio"] else "false",
                        e["fi_f"], e["fo_f"], e["pos_s"] and ("%.2fs" % e["pos_s"])))
    if not lines:
        lines.append("    -- (剪映草稿里没有淡变片段)")

    stub = os.path.splitext(os.path.basename(out_path))[0]
    src = (_LUA_TMPL
           .replace("__NAME__", os.path.basename(out_path))
           .replace("__STUB__", stub)
           .replace("__FPS__", str(fps))
           .replace("__TARGETS__", "\n".join(lines))
           .replace("__FI_KEYS__", _lua_keys(FADE_IN_KEYS))
           .replace("__FO_KEYS__", _lua_keys(FADE_OUT_KEYS)))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    # 不加 BOM: Lua 的 loadfile 虽能跳过 BOM, 但达芬奇是否是整段 loadstring 不确定,
    # 中文只出现在注释和字符串字面量里, 纯 UTF-8 即可
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    return out_path
