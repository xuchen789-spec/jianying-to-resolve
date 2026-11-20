# 剪映草稿转达芬奇（nle-bridge）

剪映 / Premiere / 达芬奇 时间线互转。

> 本仓库是项目的**唯一源码仓**，`skill/` 下是配套技能包。改动集中在这里，不要再另存副本——两份代码各自演进必然分叉。

中立时间线模型（IR）+ 多读取器 / 多写入器架构。任意两端互转，不依赖任何宿主软件，
不修改源工程，全部本地执行。

## 安装（3 步）

前置：**Python 3.9+**（Windows 安装时勾选 `Add python.exe to PATH`）。git 可选，没有也能装。

```bash
git clone https://github.com/xuchen789-spec/jianying-to-resolve.git
cd jianying-to-resolve
python tools/install_skill.py
```

Windows 上也可以直接双击仓库根目录的 `install.cmd`，效果一样。

装完打开 WorkBuddy，技能列表里会出现 `nle-bridge`（没出现就重启一次），之后直接说
「用 nle-bridge 把 D:/剪映草稿/某工程 转到达芬奇」就行。

**没有 git**：仓库页 `Code → Download ZIP`，解压后进目录跑同一条 `python tools/install_skill.py`。

**别只把 `skill/` 目录拷走。** 技能是薄壳，真正的转换逻辑在 `nlebridge/` 里，`skill/config.json`
的 `root` 必须指向完整仓库；`install_skill.py` 会自动写好这一行，手工拷目录则要自己填。

升级与卸载：

```bash
git pull                                  # 拉新版；下次运行技能自动对齐 skill/
rm -rf ~/.workbuddy/skills/nle-bridge     # 卸载（仓库和产出都不动）
```

## 转换矩阵

| 来源 \ 目标 | Premiere XML | 达芬奇 (FCPXML) | 剪映草稿 |
|---|---|---|---|
| **剪映草稿** | ✅ | ✅ | ✅（规范化/降级明文） |
| **Premiere XML** | — | ✅ | ✅（反向） |
| **达芬奇 FCPXML** | ✅（反向） | — | ✅（反向） |

> 达芬奇侧走 FCPXML 交换文件（`文件 → 导入 → 时间线`）。
> 若需直连 Resolve API 建时间线，IR 已就绪，后续加一个 `w_resolve_api.py` 即可。

## 客户端（v1.0.0）

```
python build_client.py --electron-dist <electron dist> \
                       --python-home   <便携 Python 3.12> \
                       --crypto-wheel  <pycryptodome-*.whl>
```

产出 `release/NLE Bridge 1.0.0/`（双击 `NLE Bridge.exe`）和同名 zip。界面是
Electron，转换跑在打包的便携 Python（`resources/python/`）里，用户机器上不用装
Python/Node。`resources/app/` 是明文源码，方便直接改和排查。

界面版逻辑与 `cli.py convert` 完全一致，只是把日志流成 UI。另有一个零依赖的
tkinter 版 `nlebridge_gui.py`（需要带 tkinter 的完整 Python），作为备用。

## 快速开始

```bash
# 查看结构（不写文件）
python cli.py inspect -i "D:/剪映草稿/某工程"

# 剪映 → 达芬奇
python cli.py convert -i "D:/剪映草稿/某工程" -t resolve -o out

# 剪映 → Premiere
python cli.py convert -i "D:/剪映草稿/某工程" -t pr -o out

# Premiere → 达芬奇
python cli.py convert -i "某序列.xml" -t resolve -o out

# Premiere / 达芬奇 → 剪映（反向）
python cli.py convert -i "某序列.xml" -t jianying -o out

# 导入达芬奇、打开目标时间线后：转场 + 淡变 + 蒙版 一次做完
python cli.py post -i "D:/剪映草稿/某工程" --audit   # 离线：只数数量，不用开达芬奇
python cli.py post -i "D:/剪映草稿/某工程"           # 预演：逐条对账，不写入
python cli.py post -i "D:/剪映草稿/某工程" --go      # 写入并保存

# 导入达芬奇后核对"到底进去了什么"（读工程库, 只读）
python cli.py doctor -p 示例工程 -i "D:/剪映草稿/某工程"
```

`post` 固定按 **转场 → 淡变 → 蒙版** 执行，一条命令做完，不用记三条命令也不会漏。
顺序会影响结果：淡变的画面渐显同样是 `Cross Dissolve`，先跑转场能保住该上 BCC+ 的切点。
只想跑其中几步用 `--only fades,masks`；历史工程要清空壳加 `--fix-shell`（必须最先）。

输出：

- `-t resolve` → `<名称>_resolve.fcpxml` + `<名称>.srt` + `转换报告_resolve.md`
- `-t pr` → `<名称>_premiere.xml` + `<名称>.srt` + `转换报告_premiere.md`
- `-t jianying` → `out/<名称>/`（含 `draft_content.json`、`draft_meta_info.json`），
  整个目录复制到剪映草稿目录后重启剪映即可看到工程

依赖：Python 3.8+；仅解密新版剪映草稿需要 `pycryptodome`（`pip install pycryptodome`）。
其余功能零第三方依赖（XML 用标准库解析）。

### 关于加密草稿（可选模块）

剪映 10.x 起把 `draft_content.json` 加密存储。`nlebridge/jy_crypto.py` 负责把这部分还原成明文，
它被单独拆成可选模块：**整个文件删掉也不影响其他任何功能**，`jy_read` 会自动降级为「只读未加密草稿」，
遇到加密草稿时给出提示（让你在剪映里另存为未加密版本）。见该文件顶部的用途边界说明。

## 技能包里有什么

`skill/` 是本仓库自带的技能包：`SKILL.md`（五阶段流程 + 六条硬门禁）、`references/命令手册.md`、
`references/坑位库.md`（30 条实测坑）、以及两个薄壳脚本 `nb.py` / `preflight.py`。

技能只是薄壳，真正的逻辑始终在 `nlebridge/` 里，**不会**被复制进技能目录——两份代码必然分叉。
所以 `config.json` 里那一行 `root` 是关键：它指向仓库，改代码不需要重新安装。

### 技能目录怎么保持同步

技能目录是 `skill/` 的副本。`tools/sync_skill.py` 负责把仓库单向同步过去：

```bash
python tools/sync_skill.py            # 同步（只写 SKILL.md / references / scripts）
python tools/sync_skill.py --check    # 只检查，有差异返回 1
```

`nb.py` 每次启动时会先对齐一次（`NLE_SKILL_NO_SYNC=1` 可关），`preflight.py` 的第 [5] 项
也会报出偏离。所以**改文档/改流程只改仓库 `skill/`，不要直接改技能目录**：
目标端若比仓库新（手工改过），只会告警、不会静默覆盖。

## 参与改进

用起来发现不对，两条路都行：

**报 bug** — 直接开 Issue。模板会问：卡在哪一步（`convert` / 导入达芬奇 / `post` / `fades` / `masks` / `trans` / `fix-shell`）、
剪映与达芬奇版本、完整命令、报错原文、转换报告里相关的几行、期望什么样实际什么样。
能附一个最小复现（3 秒、两三条轨的草稿）最好定位。

**已经修好了** — fork → 开分支 → 改 → 发 PR。改动请说明是哪条坑位或哪个现象触发的；
新踩到的坑欢迎直接追加到 `skill/references/坑位库.md`，那是这个项目最值钱的部分。

隐私：**不要上传客户素材、片名、完整目录路径**。贴日志前把路径打码，截图避开画面内容——
这个仓库公开，而工具处理的是别人的片子。

## 许可

MIT，见 `LICENSE`。使用前请确认：你转换的工程是**你自己拥有版权或已获授权**的素材。
`tools/` 下的审计脚本按现状提供（as-is），不对转换结果的正确性作担保。

## 已自动转换

| 属性 | 剪映 | Premiere XML | 达芬奇 FCPXML |
|---|---|---|---|
| 剪切点 / 源入出点 | `source/target_timerange` | `in/out/start/end` | `start/offset/duration` |
| 变速 | `speeds.speed` | Time Remap (`timeremap`) | `<timeMap>` |
| 位移 | `clip.transform`（半个画布 + y 向上） | Basic Motion `center`（0.5 = 中心） | `<adjust-transform position>`（**画面高度百分比**；静帧再加渲染高/2） |
| 缩放 | `clip.scale`（基于"适应画布"） | Basic Motion `scale`（%，原始像素基准） | `<adjust-transform scale>`（适应画布基准，原样传递） |
| 旋转 | `clip.rotation` | Basic Motion `rotation` | `<adjust-transform rotation>` |
| 镜像 | `clip.flip` | 负缩放 | 负 `scale` |
| 关键帧 | `common_keyframes` | `<parameter><keyframe>` | `<param><keyframeAnimation><keyframe>` |
| 音量 | `volume` | audiolevels（dB） | `audioVolume` |
| 淡入淡出 | `audio_fades` + 画面 `vfade` | 报告提示（XML 无标准表示） | `audioFadeIn/Out`；画面渐显靠在线 `Cross Dissolve`（`cli.py post`） |
| 转场 | `materials.transitions`（挂宿主**尾部**） | 报告提示 | 在线 `AddTransition`（`cli.py post` / `trans`）：叠化/闪白/闪黑用自带，推镜/放射/炫光走 BCC+；FCPXML 里只能写纯叠化/闪黑 |
| 轨道层级 | 轨序 + render_index | 独立 `<track>` | `lane`（音频负 lane） |
| 一级调色参数 | `materials.effects`（value） | `<note>` 备注 | XML 注释 |
| 文本 | `materials.texts` | SRT 字幕 | SRT 字幕 |

## 十条必须记住的格式事实（踩过的坑）

1. **FCPXML 的 `<asset>` 必须带 `duration`**，否则达芬奇把素材当成 0 长，
   整条时间线全部变成单帧。
2. **FCPXML 的变换是子元素 `<adjust-transform position scale rotation>`**，
   写成 `asset-clip` 的 `position`/`scale` 属性是非法的，会被静默忽略
   （表现为缩放/位移全丢、画面按素材原尺寸铺满）。
3. **FCPXML 的 `position` 单位是「画面高度的百分比」，不是像素**。Apple 原文
   "moved left 5 percent and up 10 percent **of the project's height**" —— 1080p 工程里
   写 645 会被解释成 645% × 1080 = 6962px，画面直接飞出画外（表现为"贴图看不见了"）。
   正确换算：`position = 像素 / 画面高 × 100`，x 也用高度作基准。
4. **剪映面板里的「位置 X/Y」是实际像素的 2 倍**。剪映内部 `transform` 以「半个画布」
   为单位，而面板显示时乘了整画布，所以面板 X=1289 ↔ 实际 645px。核对位置时别拿
   两个数字直接比，要看画面。
5. **剪映 `缩放 100%` = 素材已适应画布**，达芬奇/FCP 默认 Image Scaling 也是
   「先 contain 再乘缩放」，语义一致 ⇒ 百分比**原样传递**（剪映 20% → 达芬奇 20%）。
   PR 不同：PR 的 100% 是素材原始像素，转 PR 时才乘回适配系数。
   如果目标工程把 Image Scaling 关掉了，用 `--scale-mode native` 重新导出。
6. **主故事线（spine）必须首尾相接**，断口处要补 `<gap>`。把绝对定位的片段直接
   平铺进 spine，达芬奇会在第一个断口处停止解析（实测 177 个片段只进来 18 个）。
   而且**其余轨道不能和 spine 平级**：平级写 `lane` 会被整个忽略，目标端按"能塞进
   哪条空轨就塞哪条"重排。必须写成挂靠片段（connected clip，即主故事线元素的
   **子元素**），`lane` 才会被采纳。
   实测证据：平级写法下剪映的 6 条音轨被重排成 1/1/9/11/22/42 段，A6 里混着
   lane -1..-5 的片段 —— 这就是"音轨顺序怎么改都是反的"的真正原因。
   **挂靠片段的 `offset` 达芬奇按「时间线绝对时间」读，不按 FCPX 的「相对父元素」**。
   按规范写相对值导进达芬奇会整体错位（片段左移/重叠/被吞，就是"剪辑错误"）。
   所以默认 `--connect absolute`：全部挂到主故事线**第一个元素**下（它起点必为 0，
   相对==绝对），offset 写绝对时间，两种解释都对。给 FCPX 或严格按规范读的工具
   才用 `--connect parent`（旧名 `anchor0` 是 absolute 的别名）。
7. **音频片段引用的素材不能带视频流**。剪映"提取音频"得到的音轨往往直接指向
   原始 mp4，若在 resources 里复用同一个 `hasVideo="1"` 的 `<asset>`，达芬奇会
   无视 `srcEnable="audio"`，把这些片段当**视频片段**甩到新的视频轨上 ——
   表现为"视频轨道莫名其妙多出好几条"。正确做法：给音频用途单独声明一个
   `hasVideo="0"` 的 `<asset>`（同一路径可以有两个 asset）。
8. **静帧文件名里不能有数字**。达芬奇导入时会把 `xxx16d.png` 解析成图片序列
   `xxx[16-259215]d.png`，然后去找不存在的帧 → 丢图/离线。
   本工具默认把静帧硬链接成 `still_a.png`、`still_b.png`…（纯字母）。

9. **音轨顺序：挂靠片段的 `lane="-1"` 就是 A1**（最靠近视频那条），`-2` → A2 …
   剪映草稿里 audio 轨的数组顺序 = 界面从上到下（音频1 在最上），所以第 i 条写
   `lane=-i`。注意：这个编号**只有在挂靠片段写对了（见第 6 条）才起作用**；
   平级写 lane 时改编号怎么改都是乱的（曾误判成"故意镜像"，白改了两轮）。
10. **达芬奇按「图片底边」对齐静帧的 position**。实测：`position` 写 -31.235
   （=337px），画面里图片中心却落在 254px，差额正好是渲染高度的一半。
   所以写静帧 y 时要 **加 渲染高/2** 补偿（`--still-anchor bottom`，默认），
   否则贴图整体偏高半张图。视频素材没有这个现象。

Premiere 侧的对应事实（用 PR 自己导出的工程 XML 逐项对过）：
- Motion 效果是 `effectid=basic`；`center` 的 `<horiz>/<vert>` 是**相对画面中心的偏移**，
  `0,0` 就是居中，取值按画面宽/高归一化（实测范围 ±0.44）。写成 `0.5+x/w` 会把整条
  时间线的素材甩到右下角。
- 音频片段必须有 `<sourcetrack><mediatype>audio</mediatype>`。
- 图片的 `<stillframe>` 父元素是 **`clipitem`**，不是 `<file>`（Apple 官方 Elements 文档：
  `stillframe` 的 Parents 是 `clip`, `clipitem`）；`file` 的合法子元素只有
  `duration/rate/name/pathurl/timecode/media/width/height/mediaSource`。
- `clipitem` 里**不能出现 `<note>`**（xmeml 没有这个子元素），要留说明只能写 XML 注释。
- **PR 对多出来的元素是整份拒绝的**，而且错误框只有一句"文件导入失败"、没有原因。
  `verify.check_pr_xml` 因此加了一层"父子元素白名单"校验，用真实 PR 导出校准过。
- 另外五处也已对齐 PR 自己的导出：`<bin>` 不写自定义 id；`<sequence>` 顺序为
  `duration/rate/name/media/timecode`；`<media><audio>` 开头补齐
  `numOutputChannels/format/outputs`；音频 clipitem 不带
  `alphatype/pixelaspectratio/anamorphic`；文件换行用 LF（PR 导出也是 LF）。
- **错误消息为空不等于文件内容有问题**。PR 25 的媒体导入面板只认音视频，把 .xml
  拖进去会直接弹"文件导入失败"且原因为空；正确做法是 **File → Import (Ctrl+I)**，
  选中 .xml 后应出现「导入 XML」对话框；也可以把 .xml 拖到**项目面板**。
  `out/pr-诊断/` 里有 4 个递进对照文件（含 PR 自己导出的原样 XML），可一次定位。
  若 PR 的 XML 导入器始终不认，兜底路线是：达芬奇里把已导入正确的时间线
  **File → Export → Timeline → Final Cut Pro 7 XML**，那份 XML 由达芬奇生成，能直接进 PR。

## 导入达芬奇之后：用 doctor 核对，而不是靠肉眼

达芬奇的导入不能命令行触发，但导入结果就在工程库里：

```
%APPDATA%/Blackmagic Design/DaVinci Resolve/Support/Resolve Project Library/
    Resolve Projects/Users/<user>/Projects/<项目>/Project.db
```

`Sm2TiTrack` 是轨道、`Sm2TiItem` 是片段。`cli.py doctor` 只读地把它解开，输出：

- 视频/音频各几条轨、每条轨几个片段、时间范围
- 有没有素材被当成图片序列读入（名字里出现 `[起-止]`）
- 有没有素材离线
- 每条源轨落到了达芬奇第几条（对不上/整体倒序会直接点出来）

在达芬奇里 Ctrl+S 保存后再跑，比截图核对准得多。

## 需要人工的（报告里逐项列出）

- 剪映在线特效（美颜、磨皮、AI 色彩匹配、光效）：目标端无对应实现，附参数与参考图路径
- 蒙版（矩形 / 钢笔贝塞尔 / AI 抠像）：附宽高、羽化、是否贝塞尔轮廓，需在目标端重建
- PR 转场、Time Remap 曲线、Lumetri 调色：保留警告，不做假设性烘焙
- 视频片段自带音频的音量：PR XML 无法挂在视频轨，写入**禁用状态的备份音频轨**（数值保留、默认不发声），需要时手动启用

## 保真度自测

```bash
python test_roundtrip.py "D:/剪映草稿/某工程"
```

对样例工程（1080p24 / 6 分钟 / 172 片段 / 90 素材）实测：

- 剪映 → PR → 读回：**161/161 片段精确匹配**（视频+音频，起点与时长零偏差）
- 剪映 → FCPXML → 读回：**161/161 片段精确匹配**
- 变换、变速、音量（音频轨）全部在容差内
- 反向生成草稿可被本工具读回，轨道数、片段数、变换/变速数值一致

## 架构

```
nlebridge/
  ir.py            # Timeline / Track / Clip / Media / Transform（微秒 + 像素 + 比例）
  jy_read.py       # 剪映草稿 -> IR（自动解密、占位符路径、缺省字段兼容）
  pr_read.py       # Premiere FCP7 XML -> IR（bin clip / file ref / VideoMotion / fi / dB）
  fcpxml_read.py   # FCPXML -> IR（lane 分层 / position / speed / audioVolume）
  w_pr_xml.py      # IR -> Premiere XML
  w_fcpxml.py      # IR -> FCPXML（达芬奇）
  w_jianying.py    # IR -> 剪映草稿（反向）
  report.py        # 转换报告 + SRT
  verify.py        # 写出后自检(素材时长/单帧/音轨无音频流/静帧文件名)
  mediaprobe.py    # 容器嗅探(改错后缀) + 静帧改名(避开序列解析)
  resolve_probe.py # 直读达芬奇工程库, 核对导入结果(cli.py doctor)
cli.py             # 命令行入口
```

### 单位约定（IR）

- 时间：微秒
- 位置：像素，原点画面中心，x 向右，y **向下**
- 缩放：1.0 = 素材原始像素（剪映的"适应画布"语义在 `jy_read` 里已折算掉；
  `transform.fit_x/fit_y` 保留适配系数，写 FCPXML 时再除回去）
- 旋转：度，顺时针为正
- 关键帧：`Clip.kfs` = `{position/scale/rotation/opacity: [(片段内 us, …)]}`

## 与旧版 pr-resolve-converter 的关系

旧工具聚焦「PR → 达芬奇」并带帧级证据修复（补头补尾、时间码核对）。
新工具是通用互转内核：读取/写入分离、不修改源文件、不裁剪任何片段，
遇到无法表达的内容一律保留原值 + 写报告，而不是猜测修复。

## 已知限制（v1）

- 剪映曲线变速、PR Time Remap 曲线：不展开关键帧，按平均速度处理并警告
- 剪映位移/缩放/旋转关键帧：已写进 FCPXML（`<param><keyframeAnimation>`，时间相对
  片段起点）和 PR XML；**达芬奇是否把关键帧读进 Inspector 需要在目标端确认**，
  读不进去时按首帧静态值兜底（位置/大小是对的，只是不动）
- 剪映自带的"我在剪辑, 低于 x 元/天"推广水印（`materials/video/水印.png`）默认剔除，
  需要保留加 `--keep-watermark`
- 剪映 `target_timerange` 缺 `start` 的片段按"从 0 开始"处理（剪映对 0 值省略该字段）。
  以前把它当占位段跳过，会让整条时间线后移、片头留 3 秒空洞
- 嵌套序列 / 复合片段 / 多时间线：只取主序列
- 剪映生成的明文草稿在 5.9+ 可直接打开；更高版本会升级格式（仍保持可再读）
- FCPXML 的 `position.y` 按 FCPX 约定向上为正；若在目标端发现画面上下镜像，
  加 `--y-down` 重新导出即可
- 静帧素材的时长按"实际用到的最长源范围 + 5 秒"给，不再给几小时
  （给太长会被目标端当超长素材/序列处理）
- 静帧会硬链接成 `still_x.png`（纯字母）；不想要这个行为可用 `--no-still-rename`，
  但达芬奇大概率会把带数字的 PNG 当图片序列读
- 静帧的 y 默认按"达芬奇底边对齐"补偿了半个渲染高度（`--still-anchor bottom`）。
  若哪天达芬奇改成按中心对齐，画面会整体往下偏半张图，用 `--still-anchor center` 重导
- `out/_media_relink/` 是派生素材目录（改后缀的音频副本 + 改名的静帧），
  整个输出目录要一起挪动，否则这几条会离线
- 挂靠片段的 `offset`：达芬奇按**绝对时间**读，所以默认 `--connect absolute`
  （全部挂在主故事线第一个元素下，相对==绝对）。给 FCPX/严格按规范读的工具时用
  `--connect parent`（相对父元素），但**达芬奇导这个会错位**，别混用
- 注意：导入前先删掉达芬奇里的旧时间线。同一个项目里两条同名时间线很容易看错，
  "剪辑错误"有时其实是在看上一版
