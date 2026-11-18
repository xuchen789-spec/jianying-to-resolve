# -*- coding: utf-8 -*-
"""中立时间线模型 (IR)

所有读取器把各自格式转成 IR，所有写入器从 IR 生成目标格式。
单位约定:
  - 时间: 微秒 (us)
  - 位置: 像素, 原点=画面中心, x 向右, y 向下 (与 Premiere/AE 一致)
  - 缩放: 1.0 = 素材原始像素; 剪映的"适应画布"语义在读入时折算为等价像素缩放
  - 旋转: 度, 顺时针为正
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Media:
    id: str
    path: str
    kind: str = "video"          # video | audio | image
    duration_us: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    name: str = ""
    missing: bool = False


@dataclass
class Transform:
    x: float = 0.0               # 像素, 相对画面中心
    y: float = 0.0               # 像素, 向下为正
    scale_x: float = 1.0         # 相对素材原始像素 (PR 语义)
    scale_y: float = 1.0
    fit_x: float = 1.0           # 该素材 occupy 画布的 contain 系数
    fit_y: float = 1.0           # scale / fit = "相对适应画布"的缩放 (剪映/达芬奇默认语义)
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    opacity: float = 1.0

    def is_identity(self) -> bool:
        return (abs(self.x) < 0.01 and abs(self.y) < 0.01
                and abs(self.scale_x - 1) < 1e-4 and abs(self.scale_y - 1) < 1e-4
                and abs(self.rotation) < 0.01 and not self.flip_h and not self.flip_v
                and abs(self.opacity - 1) < 1e-4)


@dataclass
class Clip:
    media_id: str
    start: int                   # 时间线起点 us
    dur: int                     # 时间线长度 us
    src_in: int = 0              # 素材内入点 us
    src_dur: int = 0             # 素材内消耗长度 us (未变速时通常 == dur)
    speed: float = 1.0
    transform: Transform = field(default_factory=Transform)
    volume: float = 1.0
    fade_in: int = 0
    fade_out: int = 0
    # 视频画面的叠化(剪映 入场「渐显」/出场「渐隐」动画, µs), 与声音淡变相互独立
    vfade_in: int = 0
    vfade_out: int = 0
    own_audio: bool = False      # 该片段自带声音(剪映视频轨未"分离音频"), 音轨上不重复生成
    grade: Dict[str, float] = field(default_factory=dict)   # 剪映一级调色滑块
    effects: List[str] = field(default_factory=list)         # 需人工重建的片上效果
    masks: List[dict] = field(default_factory=list)          # 蒙版描述
    text: Optional[str] = None                               # 文本内容(去标签)
    label: str = ""                                          # 来源片段名/ID, 用于 ColorTrace 匹配
    source_app: str = ""
    # 关键帧: key ∈ {position, scale, rotation, opacity}
    #   position -> [(相对片段起点的 us, x_px, y_px)]
    #   scale    -> [(us, scale_x, scale_y)]  (相对素材原始像素)
    #   rotation -> [(us, deg)] , opacity -> [(us, 0~1)]
    kfs: Dict[str, List[tuple]] = field(default_factory=dict)

    @property
    def end(self) -> int:
        return self.start + self.dur


@dataclass
class Track:
    kind: str                    # video | audio | text
    clips: List[Clip] = field(default_factory=list)
    name: str = ""

    @property
    def size(self) -> int:
        return len(self.clips)


@dataclass
class Timeline:
    name: str = "timeline"
    width: int = 1920
    height: int = 1080
    fps: int = 24
    tracks: List[Track] = field(default_factory=list)
    media: Dict[str, Media] = field(default_factory=dict)
    source_app: str = ""
    # 视频片段自带声音的承载方式: 'track' = 独立 "Vn 原声" 音轨; 'video' = 挂在视频片段上
    video_audio_mode: str = "track"
    warnings: List[str] = field(default_factory=list)
    manual_items: List[str] = field(default_factory=list)
    # 转场: [{"at": 切点us(=宿主段尾), "dur": 时长us, "name": 原名,
    #         "host_start": 宿主段起点us, "host_dur": 宿主段长度us}]
    # 注意: 剪映把转场挂在**宿主片段尾部**, at = host_start + host_dur。
    transitions: List[dict] = field(default_factory=list)

    # ---- 便捷访问 ----
    def add_media(self, m: Media) -> Media:
        self.media.setdefault(m.id, m)
        return self.media[m.id]

    def video_tracks(self) -> List[Track]:
        return [t for t in self.tracks if t.kind == "video"]

    def audio_tracks(self) -> List[Track]:
        return [t for t in self.tracks if t.kind == "audio"]

    def text_tracks(self) -> List[Track]:
        return [t for t in self.tracks if t.kind == "text"]

    @property
    def duration(self) -> int:
        return max((c.end for t in self.tracks for c in t.clips), default=0)

    def clip_count(self) -> int:
        return sum(len(t.clips) for t in self.tracks)

    def summary(self) -> str:
        parts = [f"{self.name}: {self.width}x{self.height}@{self.fps}fps "
                 f"{self.duration/1e6:.2f}s"]
        for t in self.tracks:
            parts.append(f"  {t.kind}: {len(t.clips)} 片段")
        parts.append(f"  素材: {len(self.media)} (缺失 {sum(1 for m in self.media.values() if m.missing)})")
        return "\n".join(parts)


# ---------------- 单位换算 ----------------
def frames_to_us(frames: int, fps: int) -> int:
    return int(round(frames * 1_000_000 / fps))


def us_to_frames(us: int, fps: int) -> int:
    return int(round(us * fps / 1_000_000))


def jianying_grade_params(tl: Timeline) -> dict:
    """汇总调色参数组的出现次数(报告用)"""
    from collections import Counter
    import json
    c = Counter()
    for t in tl.tracks:
        for cl in t.clips:
            if cl.grade:
                c[json.dumps(cl.grade, sort_keys=True, ensure_ascii=False)] += 1
    return dict(c)
