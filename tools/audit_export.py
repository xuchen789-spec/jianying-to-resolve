# -*- coding: utf-8 -*-
"""离线审计: 把导出的 _resolve.fcpxml 和剪映草稿对账(不需要启动达芬奇)。

检查项
  1. 结构: 视频/音频段数、轨道分布、总长
  2. 边界: 片段 offset/duration 是否等于草稿 target_timerange
  3. 速度: timeMap 推出的倍速是否等于草稿 speed
  4. 入点: 达芬奇导入时做 in_effective = in_export × speed(见文档坑 1),
     所以期望 in_export = 草稿源入点 / speed —— 这里直接预测导入结果并报误差
  5. 缺段: 草稿有、导出没有

用法
  python tools/audit_export.py
  python tools/audit_export.py --fcpxml X --draft Y --verbose
  python tools/audit_export.py --json

退出码: 0 = 全绿, 1 = 有需修项
"""
import argparse
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEF_FCPXML = os.path.join(HERE, 'out', 'timeline_resolve.fcpxml')
DEF_DRAFT = os.path.join(HERE, 'out', 'draft_content_decrypted.json')
FPS = 24.0                     # 时间线帧率(判定以秒为主, 帧只用于报数)
TOL_FRAME = 1.0
TOL_IN_S = 0.02                # ≈ 0.5 帧
TOL_SPEED = 0.005


def parse_time(s):
    if s is None:
        return 0.0
    s = s.strip().rstrip('s')
    if '/' in s:
        a, b = s.split('/')
        try:
            return float(a) / float(b) if float(b) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
TOK_RE = re.compile(r'<clip\b([^>]*?)>|</clip>|<asset-clip\b([^>]*?)(/?)>', re.S)


def parse_clips(text):
    """线性扫描, 用 <clip lane>-N </clip> 的嵌套关系给音频片段定轨。"""
    stack = []           # 当前所属 storyline 的 lane
    out = []
    for m in TOK_RE.finditer(text):
        if m.group(1) is not None:                       # <clip ...>
            a = dict(ATTR_RE.findall(m.group(1)))
            stack.append(int(a['lane']) if a.get('lane') not in (None, '') else None)
            continue
        if m.re is not None and m.group(0) == '</clip>':
            if stack:
                stack.pop()
            continue
        a = dict(ATTR_RE.findall(m.group(2) or ''))      # <asset-clip ...>
        body = ''
        if not m.group(3):
            end = text.find('</asset-clip>', m.end())
            body = text[m.end():end if end > 0 else m.end()]
        tm = re.findall(r'<timept\b[^>]*?time="([^"]+)"[^>]*?value="([^"]+)"', body)
        own = a.get('lane')
        lane = int(own) if own not in (None, '') else (stack[-1] if stack else None)
        out.append({
            'ref': a.get('ref'),
            'audio': (a.get('srcEnable') == 'audio'),
            'lane': lane,
            'offset_s': parse_time(a.get('offset')),
            'start_s': parse_time(a.get('start')),
            'dur_s': parse_time(a.get('duration')),
            'src_s': parse_time(tm[-1][1]) if tm else 0.0,
            'has_tm': bool(tm),
        })
    return out


def parse_assets(text):
    d = {}
    for m in re.finditer(r'<asset\b([^>]*?)/?>', text):
        a = dict(ATTR_RE.findall(m.group(1)))
        if a.get('id'):
            d[a['id']] = a.get('name') or ''
    return d


def key_of(name):
    """素材名归一: 去目录/去扩展名/取前 12 字符, 兼顾剪映原名与哈希名。"""
    n = os.path.basename((name or '').replace('\\', '/'))
    n = re.sub(r'\.\w+$', '', n)
    return n[:12]


def load_draft(path):
    """草稿可以是目录(剪映加密草稿)、明文 json 或加密 draft_content.json。"""
    sys.path.insert(0, HERE)
    if os.path.isdir(path) or not path.lower().endswith('.json'):
        from nlebridge.jy_read import load_draft_json
        D, _base = load_draft_json(path)
    else:
        try:
            D = json.load(open(path, encoding='utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            from nlebridge.jy_read import load_draft_json
            D, _base = load_draft_json(path)
    mats = D.get('materials', {})
    nm = {}
    for k in ('videos', 'audios'):
        for m in mats.get(k, []):
            if isinstance(m, dict) and m.get('id'):
                nm[m['id']] = m.get('material_name') or os.path.basename(m.get('path', ''))
    rows = []
    for kind in ('video', 'audio'):
        for ti, tr in enumerate([t for t in D['tracks'] if t.get('type') == kind]):
            for s in tr.get('segments', []):
                tt = s.get('target_timerange') or {}
                # 剪映对"从第 0 帧开始"的段不写 start 键(不是数据缺失)。
                # 早先按缺键跳过, 导致这些段被判成"导出多余", 误报 @0 的补头。
                if not tt.get('duration'):
                    continue
                sr = s.get('source_timerange') or {}
                dur_s = tt.get('duration', 0) / 1e6
                src_dur = sr.get('duration', 0) / 1e6
                spd = (src_dur / dur_s) if dur_s > 0 else (s.get('speed', 1.0) or 1.0)
                rows.append({'kind': kind, 'track': ti + 1,
                             'name': nm.get(s.get('material_id'), '?'),
                             'off_s': tt.get('start', 0) / 1e6, 'dur_s': dur_s,
                             'in_s': sr.get('start', 0) / 1e6, 'speed': spd})
    return rows


def _geom_ok(d, clip):
    return (abs(d['off_s'] - clip['offset_s']) * FPS <= 2.0
            and abs(d['dur_s'] - clip['dur_s']) * FPS <= 2.0)


def _covered_in_draft(d, pool):
    """这段是否被草稿里另一段(同素材、同源入点、完全包住)盖掉。

    剪映草稿经常在同一时刻同时放着"21 帧短条 + 80 帧长条", 内容一模一样。
    读入时去重规则会丢掉被包住的那条(正确), 审计不该把它报成缺段。
    """
    for e in pool:
        if e is d or e['name'] != d['name']:
            continue
        if abs(e['in_s'] - d['in_s']) > TOL_IN_S:
            continue
        if e['off_s'] <= d['off_s'] + 1e-6 and e['off_s'] + e['dur_s'] >= d['off_s'] + d['dur_s'] - 1e-6:
            if e['dur_s'] > d['dur_s'] + 1e-6:
                return True
    return False


def match_all(clips, pool, track_of):
    """两遍配对, 且**按轨匹配优先**。

    轨映射是确定的: spine = 主故事线(段最多那条), 视频连接片段 lane k = 主轨之后
    第 k 条轨; 音频连接片段 lane -N = 剪映第 N 条音轨(写入器就是这么写的)。
    不同轨常有"同起点同长度"的段, 只按几何配对会互相串(出现对称的 ±x 假误差)。

    两遍: ①同名 ②仅几何。不能用"同名即配对"(管道会把 png 静帧转成 mp4), 也不能
    只看几何(跨轨撞车)。返回 ({clip_idx: draft_idx}, 已用 draft_idx 集合)。
    """
    bytrk = {}
    for j, d in enumerate(pool):
        bytrk.setdefault(d['track'], []).append(j)
    pairs, used = {}, set()
    for pass_no in (1, 2):
        for i, c in enumerate(clips):
            if i in pairs:
                continue
            trk = track_of(c)
            order = bytrk.get(trk, []) + [j for j in range(len(pool))
                                         if pool[j]['track'] != trk]
            kc = key_of(c['name'])
            best, bests = None, 1e9
            for j in order:
                if j in used:
                    continue
                d = pool[j]
                if not _geom_ok(d, c):
                    continue
                if pass_no == 1 and key_of(d['name']) != kc:
                    continue
                # 同轨优先 + 起点最近
                s = abs(d['off_s'] - c['offset_s']) + (0.0 if d['track'] == trk else 0.01)
                if s < bests:
                    best, bests = j, s
            if best is not None:
                pairs[i] = best
                used.add(best)
    return pairs, used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fcpxml', default=DEF_FCPXML)
    ap.add_argument('--draft', default=DEF_DRAFT)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--verbose', action='store_true', help='打印所有片段(含 1x)')
    ap.add_argument('--strict', action='store_true',
                    help='缺段/多余段也算失败(默认只把入点/速度/边界当失败)')
    args = ap.parse_args()

    if not os.path.exists(args.fcpxml):
        print('找不到 fcpxml:', args.fcpxml)
        return 1
    text = open(args.fcpxml, encoding='utf-8').read()
    assets = parse_assets(text)
    clips = parse_clips(text)
    draft = load_draft(args.draft)
    vd = [d for d in draft if d['kind'] == 'video']
    ad = [d for d in draft if d['kind'] == 'audio']

    used = {'video': set(), 'audio': set()}
    rows, problems = [], []
    groups = {'video': [c for c in clips if not c['audio']],
              'audio': [c for c in clips if c['audio']]}
    # 音频轨上会有"视频自带声音"(linked audio): 草稿里没有独立音频段, 只能跟
    # 同位置的视频段对账 —— 先按草稿音频段配对, 剩下的再试视频段, 都试不上才算多余
    linked_checked = linked_skipped = 0
    vtrks = sorted({d['track'] for d in vd})
    prim = (max(vtrks, key=lambda t: sum(1 for d in vd if d['track'] == t))
            if vtrks else 1)
    non_prim = [t for t in vtrks if t != prim]

    def track_of(c, kind):
        """导出的 lane → 草稿轨号。"""
        lane = c['lane']
        if kind == 'audio':
            return -lane if (lane or 0) < 0 else 0
        if lane in (None, 0):
            return prim
        k = int(lane)
        return non_prim[k - 1] if 0 < k <= len(non_prim) else prim

    for kind, glist in groups.items():
        pool = ad if kind == 'audio' else vd
        for c in glist:
            c['name'] = assets.get(c['ref'], '?')
        pairs, used_d = match_all(glist, pool, lambda c, k=kind: track_of(c, k))
        used[kind] |= used_d
        leftover = []
        for i, c in enumerate(glist):
            name = c['name']
            if i not in pairs:
                leftover.append(c)
                continue
            d = pool[pairs[i]]
            got = (c['src_s'] / c['dur_s']) if (c['has_tm'] and c['dur_s']) else 1.0
            eff_in = c['start_s'] * got                      # 达芬奇导入后的真实入点
            bnd = (c['offset_s'] - d['off_s']) * FPS
            in_err = eff_in - d['in_s']
            spd_err = got - d['speed']
            flags = []
            if abs(bnd) > TOL_FRAME:
                flags.append('边界%+.0f帧' % bnd)
                problems.append({'type': 'boundary', 'kind': kind, 'lane': d['track'],
                                 'offset_f': round(d['off_s'] * FPS), 'err_f': round(bnd, 1),
                                 'name': d['name']})
            if abs(spd_err) > TOL_SPEED:
                flags.append('速度%.3f≠%.3f' % (got, d['speed']))
                problems.append({'type': 'speed', 'kind': kind, 'lane': d['track'],
                                 'offset_f': round(d['off_s'] * FPS), 'got': round(got, 3),
                                 'want': round(d['speed'], 3), 'name': d['name']})
            if abs(in_err) > TOL_IN_S:
                flags.append('入点%+.2fs' % in_err)
                problems.append({'type': 'inpoint', 'kind': kind, 'lane': d['track'],
                                 'offset_f': round(d['off_s'] * FPS), 'err_s': round(in_err, 3),
                                 'err_f': round(in_err * FPS), 'name': d['name'],
                                 'speed': round(got, 3), 'in_export_s': round(c['start_s'], 3),
                                 'in_expected_s': round(d['in_s'], 3)})
            rows.append({'kind': kind, 'lane': d['track'], 'off_f': round(d['off_s'] * FPS),
                         'dur_f': round(d['dur_s'] * FPS), 'name': d['name'], 'speed': d['speed'],
                         'got': got, 'eff_in': eff_in, 'in_s': d['in_s'], 'flags': flags})

        # ---- 配对不上的: 先试"视频自带声音", 再判定为多余 ----
        for c in leftover:
            cands = [d for d in vd if _geom_ok(d, c)]
            if not cands:
                problems.append({'type': 'unmatched', 'kind': kind, 'lane': c['lane'] or 0,
                                 'offset_f': round(c['offset_s'] * FPS), 'name': c['name'],
                                 'dur_f': round(c['dur_s'] * FPS)})
                continue
            kc = key_of(c['name'])
            same = [d for d in cands if key_of(d['name']) == kc]
            got = (c['src_s'] / c['dur_s']) if (c['has_tm'] and c['dur_s']) else 1.0
            if not same:
                # 素材名对不上是常态: 剪映草稿里存的是原名(35.mp4), 导出用的是媒体池
                # 里的哈希文件名(520326aa….mp4)。这种"同位置叠了好几层"的情况, 只要
                # 这一摞里有入点吻合的视频段, 它就是那条的原声 —— 不算偏移。
                if any(abs(c['start_s'] * got - d['in_s']) <= TOL_IN_S for d in cands):
                    linked_checked += 1
                    continue
            d = min(same or cands, key=lambda x: abs(x['off_s'] - c['offset_s']))
            linked_checked += 1
            in_err = c['start_s'] * got - d['in_s']
            if abs(in_err) > TOL_IN_S:
                problems.append({'type': 'linked', 'kind': 'audio', 'lane': d['track'],
                                 'offset_f': round(d['off_s'] * FPS), 'name': d['name'],
                                 'err_s': round(in_err, 3), 'err_f': round(in_err * FPS)})

    # 草稿有、导出没有
    merged = []          # 草稿自身就重复(被另一段同素材同入点完全包住), 去重已吸收
    stripped = []        # 转换时默认剔除的层(剪映推广水印)
    for kind, pool in (('video', vd), ('audio', ad)):
        for i, d in enumerate(pool):
            if i not in used[kind]:
                if _covered_in_draft(d, pool):
                    merged.append(d)
                    continue
                if '水印' in (d['name'] or ''):
                    stripped.append(d)
                    continue
                problems.append({'type': 'missing', 'kind': kind, 'lane': d['track'],
                                 'offset_f': round(d['off_s'] * FPS), 'name': d['name'],
                                 'dur_f': round(d['dur_s'] * FPS)})

    hard = [p for p in problems if p['type'] in ('inpoint', 'speed', 'boundary')]
    soft = [p for p in problems if p['type'] not in ('inpoint', 'speed', 'boundary')]
    fail = bool(hard) or (args.strict and bool(soft))

    if args.json:
        print(json.dumps({'clips': len(clips), 'draft': len(draft),
                          'problems': problems, 'fail': fail}, ensure_ascii=False, indent=2))
        return 1 if fail else 0

    nv = sum(1 for c in clips if not c['audio'])
    na = len(clips) - nv
    print('fcpxml: %s' % os.path.basename(args.fcpxml))
    print('导出: 视频 %d / 音频 %d 条 (共 %d) | 草稿: 视频 %d / 音频 %d 段'
          % (nv, na, len(clips), len(vd), len(ad)))

    # ---- 明细(默认只列变速段或有问题段) ----
    print('\n%-4s %-8s %-7s %-20s %-6s %-6s %-9s %-9s %s'
          % ('轨', '起点', '长度', '素材', '草速', '实速', '导入in(s)', '期望in(s)', '判定'))
    shown = 0
    for r in rows:
        if not args.verbose and not r['flags'] and abs(r['speed'] - 1.0) < 1e-4:
            continue
        shown += 1
        print('%-4s %-8d %-7d %-20s %-6.3f %-6.3f %-9.3f %-9.3f %s'
              % ('A%d' % r['lane'] if r['kind'] == 'audio' else 'V%d' % r['lane'],
                 r['off_f'], r['dur_f'], (r['name'] or '?')[:20], r['speed'], r['got'],
                 r['eff_in'], r['in_s'], ('⚠ ' + '; '.join(r['flags'])) if r['flags'] else 'OK'))
    if not shown:
        print('  (无非 1x 段, 且全部 1x 入点精确)')

    # ---- 汇总 ----
    by_t = {}
    for p in problems:
        by_t.setdefault(p['type'], []).append(p)
    print('\n== 汇总 ==')
    print('  视频段 %d (草稿 %d) | 音频段 %d (草稿 %d)'
          % (nv, len(vd), na, len(ad)))
    if merged or stripped:
        bits = []
        if merged:
            bits.append('草稿自身重复 %d 项(去重已吸收)' % len(merged))
        if stripped:
            bits.append('默认剔除水印层 %d 项' % len(stripped))
        print('  已解释: ' + '、'.join(bits))
    if not problems:
        print('  全绿: 边界/速度/入点均与草稿一致')
        return 0

    names = {'inpoint': '入点偏移', 'speed': '速度不符', 'boundary': '边界偏移',
             'missing': '缺段', 'unmatched': '多余段(草稿无)',
             'linked': '自带声音入点偏(随视频同源问题)'}
    for t in ('inpoint', 'speed', 'boundary', 'missing', 'unmatched', 'linked'):
        lst = by_t.get(t) or []
        if not lst:
            continue
        print('  %s: %d 项' % (names[t], len(lst)))
        for p in lst:
            tag = 'A' if p.get('kind') == 'audio' else 'V'
            if t == 'inpoint':
                print('    %s%d @%df %-18s 速%.2f  导出in=%.3fs → 导入后%.3fs, 期望%.3fs (差%+d帧)'
                      % (tag, p['lane'], p['offset_f'], p['name'][:18], p['speed'],
                         p['in_export_s'], p['in_export_s'] * p['speed'], p['in_expected_s'], p['err_f']))
            elif t == 'speed':
                print('    %s%d @%df %-18s 实%.3f 期望%.3f'
                      % (tag, p['lane'], p['offset_f'], p['name'][:18], p['got'], p['want']))
            elif t == 'boundary':
                print('    %s%d @%df %-18s %+.1f 帧'
                      % (tag, p['lane'], p['offset_f'], p['name'][:18], p['err_f']))
            elif t == 'linked':
                print('    V%d @%df %-18s 音频入点 %+d 帧'
                      % (p['lane'], p['offset_f'], p['name'][:18], p['err_f']))
            else:
                print('    %s%d @%df %-18s %d帧'
                      % (tag, p['lane'], p['offset_f'], p['name'][:18], p.get('dur_f') or 0))
    if hard:
        print('  判定: 失败(入点/速度/边界 %d 项)' % len(hard))
    else:
        print('  判定: 通过(仅 %d 项缺段/多余段提示, 需 --strict 才当失败)' % len(soft))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
