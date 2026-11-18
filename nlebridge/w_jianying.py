# -*- coding: utf-8 -*-
"""IR → 剪映草稿 (反向路线: PR / 达芬奇 → 剪映)

生成明文草稿 draft_content.json + draft_meta_info.json，剪映打开时自动补全其余文件。
生成规范参考剪映 5.9 结构，可在 5.9 及以上版本打开（新版会升级但保持明文可再读）。
时间单位: 微秒; 变换: clip.transform 为画布归一化值(-1~1)，scale 为相对画幅比例
"""
import json
import os
import time
import uuid

from .ir import Timeline

APP = {"app_id": 3704, "app_source": "lv", "app_version": "5.9.0", "device_id": "",
       "hard_disk_id": "", "mac_address": "", "os": "windows", "os_version": "10.0.22621"}


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def _video_material(m, path):
    return {
        "id": m.id, "type": "video", "path": path, "material_name": m.name or os.path.basename(path),
        "duration": m.duration_us, "width": m.width or 1920, "height": m.height or 1080,
        "has_audio": bool(m.has_audio), "category_name": "local", "source_platform": 0,
        "create_time": 0, "import_time": 0, "import_time_ms": 0, "item_source": 1,
        "extra_type_option": 0, "crop": {
            "lower_left_x": 0.0, "lower_left_y": 0.0, "lower_right_x": 1.0,
            "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
            "upper_right_x": 1.0, "upper_right_y": 1.0},
        "crop_ratio": "free", "crop_scale": 1.0, "crop_flip": False,
        "matting": {"flag": 0, "has_use_quick_brush": False, "has_use_quick_eraser": False,
                    "interactiveTime": [], "path": "", "strokes": []},
        "video_algorithm": {"algorithms": [], "complement_frame_config": None,
                            "denoise": None, "deflicker": None, "gameplay_configs": [],
                            "motion_blur_config": None, "noise_reduction": None,
                            "path": "", "quality_enhance": None, "smart_complement_frame": None,
                            "super_resolution": None, "time_range": None},
        "aigc_type": "none", "is_ai_generate_content": False, "local_material_id": "",
    }


def _audio_material(m, path):
    return {
        "id": m.id, "type": "extract_music", "path": path,
        "name": m.name or os.path.basename(path), "duration": m.duration_us,
        "music_id": "", "source_platform": 0, "category_name": "local",
        "wave_points": [], "request_id": "", "team_id": "",
        "app_id": 0, "formula_id": "", "is_ai_generate_content": False,
    }


def _text_material(text: str):
    mid = _uid()
    content = json.dumps({"text": text, "styles": [{
        "fill": {"content": {"render_type": "solid",
                             "solid": {"color": [1.0, 1.0, 1.0]}}},
        "range": [0, len(text)],
        "size": 30.0, "font": {"path": "", "id": ""},
    }]}, ensure_ascii=False)
    return {
        "id": mid, "type": "text", "content": content,
        "font_size": 30.0, "text_color": "#FFFFFF", "text_size": 30,
        "border_color": "#000000", "border_width": 0.0, "border_alpha": 1.0,
        "alignment": 1, "letter_spacing": 0.0, "line_spacing": 0.02,
    }


def write(tl: Timeline, out_dir: str, draft_name: str = None) -> str:
    fps = float(tl.fps)
    name = draft_name or tl.name
    os.makedirs(out_dir, exist_ok=True)

    canvas_id = _uid()
    materials = {
        "videos": [], "audios": [], "speeds": [], "canvases": [{
            "id": canvas_id, "type": "canvas_color", "color": "",
            "blur": 0.0, "album_image": "", "image": "", "image_id": "",
            "image_name": "", "source_platform": 0, "team_id": ""}],
        "audio_fades": [], "texts": [], "effects": [], "material_animations": [],
        "masks": [], "transitions": [], "beats": [], "sound_channel_mappings": [],
        "speeds": [], "placeholders": [], "hsl": [], "vocal_separations": [],
        "material_colors": [], "loudnesses": [], "time_marks": [],
    }
    tracks = []
    written = {"video": 0, "audio": 0, "text": 0}

    def canvas_ref():
        return canvas_id

    def speed_ref(sp: float):
        if abs(sp - 1.0) < 1e-4:
            return None
        sid = _uid()
        materials["speeds"].append({
            "id": sid, "type": "speed", "mode": 0, "speed": float(sp),
            "curve_speed": None, "curve_speed_boundary": None})
        return sid

    def fade_ref(fi: int, fo: int):
        if not fi and not fo:
            return None
        fid = _uid()
        materials["audio_fades"].append({
            "id": fid, "type": "audio_fade",
            "fade_in_duration": int(fi), "fade_out_duration": int(fo)})
        return fid

    def segment(mat_id, c, extra, kind_extra_always):
        # 剪映: transform 单位 = 半个画布, y 轴向上; scale 相对"适应画布"
        fit = c.transform.fit_x or 1.0
        clip = {
            "alpha": 1.0 if c.transform.opacity >= 1 else c.transform.opacity,
            "flip": {"horizontal": c.transform.flip_h, "vertical": c.transform.flip_v},
            "rotation": c.transform.rotation,
            "scale": {"x": c.transform.scale_x / fit, "y": c.transform.scale_y / fit},
            "transform": {"x": c.transform.x / (tl.width / 2),
                          "y": -c.transform.y / (tl.height / 2)},
        }
        return {
            "id": _uid(), "material_id": mat_id,
            "source_timerange": {"start": int(c.src_in), "duration": int(max(c.src_dur, 1))},
            "target_timerange": {"start": int(c.start), "duration": int(max(c.dur, 1))},
            "extra_material_refs": [r for r in extra if r],
            "clip": clip,
            "uniform_scale": {"on": True, "value": 1.0},
            "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
            "render_index": 0, "track_render_index": 0, "render_timerange": {},
            "speed": float(c.speed), "volume": float(c.volume),
            "last_nonzero_volume": float(c.volume) if c.volume > 0 else 1.0,
            "visible": True, "reverse": False, "is_placeholder": False,
            "enable_adjust": True, "enable_lut": True, "enable_color_curves": True,
            "enable_color_wheels": True, "enable_smart_color_adjust": False,
            "enable_color_correct_adjust": False, "enable_color_match_adjust": False,
            "intensifies_audio": False, "is_tone_modify": False,
            "common_keyframes": [], "keyframe_refs": [],
            "template_id": "", "template_scene": "default",
            "caption_info": None, "cartoon": False, "group_id": "",
            **kind_extra_always,
        }

    # 视频轨 (IR 底→顶 == 剪映 轨序)
    for li, tr in enumerate(tl.video_tracks()):
        segs = []
        for c in sorted(tr.clips, key=lambda c: c.start):
            m = tl.media.get(c.media_id)
            if not m or m.missing:
                continue
            vm = _video_material(m, os.path.abspath(m.path))
            materials["videos"].append(vm)
            extra = [canvas_ref(), speed_ref(c.speed), fade_ref(c.fade_in, c.fade_out)]
            s = segment(vm["id"], c, extra, {})
            s["render_index"] = li
            s["track_render_index"] = li
            segs.append(s)
            written["video"] += 1
        if segs:
            tracks.append({"id": _uid(), "type": "video", "attribute": 0, "flag": 0,
                           "is_default_name": True, "name": "", "segments": segs})

    # 音频轨
    for li, tr in enumerate(tl.audio_tracks()):
        segs = []
        for c in sorted(tr.clips, key=lambda c: c.start):
            m = tl.media.get(c.media_id)
            if not m or m.missing:
                continue
            am = _audio_material(m, os.path.abspath(m.path))
            materials["audios"].append(am)
            extra = [speed_ref(c.speed), fade_ref(c.fade_in, c.fade_out)]
            s = segment(am["id"], c, extra, {})
            s["render_index"] = 0
            segs.append(s)
            written["audio"] += 1
        if segs:
            tracks.append({"id": _uid(), "type": "audio", "attribute": 0, "flag": 0,
                           "is_default_name": True, "name": "", "segments": segs})

    # 文本轨
    for tr in tl.text_tracks():
        segs = []
        for c in sorted(tr.clips, key=lambda c: c.start):
            if not c.text:
                continue
            tm = _text_material(c.text)
            materials["texts"].append(tm)
            s = {
                "id": _uid(), "material_id": tm["id"],
                "source_timerange": {"start": 0, "duration": int(c.dur)},
                "target_timerange": {"start": int(c.start), "duration": int(c.dur)},
                "extra_material_refs": [], "clip": {
                    "alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
                    "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0},
                    "transform": {"x": 0.0, "y": 0.0}},
                "uniform_scale": {"on": True, "value": 1.0},
                "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
                "render_index": 0, "track_render_index": 0, "render_timerange": {},
                "speed": 1.0, "volume": 1.0, "last_nonzero_volume": 1.0,
                "visible": True, "reverse": False, "is_placeholder": False,
                "enable_adjust": False, "enable_lut": False, "enable_color_curves": False,
                "enable_color_wheels": False, "enable_smart_color_adjust": False,
                "enable_color_correct_adjust": False, "enable_color_match_adjust": False,
                "intensifies_audio": False, "is_tone_modify": False,
                "common_keyframes": [], "keyframe_refs": [],
                "template_id": "", "template_scene": "default",
                "caption_info": None, "cartoon": False, "group_id": "",
            }
            segs.append(s)
            written["text"] += 1
        if segs:
            tracks.append({"id": _uid(), "type": "text", "attribute": 0, "flag": 0,
                           "is_default_name": True, "name": "", "segments": segs})

    draft_id = _uid()
    content = {
        "id": draft_id, "version": 360000, "new_version": "90.0.0",
        "fps": fps, "duration": int(tl.duration),
        "canvas_config": {"width": tl.width, "height": tl.height, "ratio": "original"},
        "color_space": 0,
        "create_time": 0, "update_time": int(time.time()), "cover": None,
        "extra_info": None, "free_render_index_mode_on": False,
        "group_container": None, "keyframe_graph_list": [],
        "keyframes": {"adjusts": [], "audios": [], "effects": [], "filters": [],
                      "handwrites": [], "stickers": [], "texts": [], "videos": []},
        "last_modified_platform": APP, "platform": APP,
        "mutable_config": None, "name": "", "relationships": [],
        "render_index_track_mode_on": False, "retouch_cover": None,
        "source": "default", "static_cover_image_path": "", "time_marks": None,
        "materials": materials, "tracks": tracks, "config": {
            "adjust_max_index": 1, "attachment_info": [], "combination_max_index": 1,
            "export_range": None, "extract_audio_last_index": 1,
            "lyrics_recognition_id": "", "lyrics_sync": True, "lyrics_taskinfo": [],
            "maintrack_adsorb": True, "material_save_mode": 0,
            "multi_language_current": "none", "multi_language_list": [],
            "multi_language_main": "none", "multi_language_mode": "none",
            "original_sound_last_index": 1, "record_audio_last_index": 1,
            "sticker_max_index": 1, "subtitle_keywords_config": None,
            "subtitle_recognition_id": "", "subtitle_sync": True, "subtitle_taskinfo": [],
            "system_font_list": [], "video_mute": False, "zoom_info_params": None},
    }

    draft_path = os.path.join(out_dir, "draft_content.json")
    with open(draft_path, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

    meta = {
        "draft_cloud_capcut_purchase_info": "", "draft_cloud_last_action_download": False,
        "draft_cloud_materials": [], "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "draft_cover.jpg", "draft_fold_path": os.path.abspath(out_dir),
        "draft_id": draft_id, "draft_is_ai_shorts": False, "draft_is_invisible": False,
        "draft_json_file": os.path.abspath(draft_path), "draft_name": name,
        "draft_new_version": "", "draft_root_path": os.path.dirname(os.path.abspath(out_dir)),
        "draft_timeline_materials_size": 0, "draft_type": "",
        "tm_draft_create": int(time.time() * 1_000_000),
        "tm_draft_modified": int(time.time() * 1_000_000),
        "tm_duration": int(tl.duration),
    }
    with open(os.path.join(out_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    with open(os.path.join(out_dir, "_converted.json"), "w", encoding="utf-8") as f:
        json.dump({"source_app": tl.source_app, "counts": written,
                   "duration": tl.duration, "fps": tl.fps}, f, ensure_ascii=False, indent=2)
    return draft_path
