# selfsr 交接说明（2026-10-28，BH3GEI）

回答平台接手时提的问题。报告和全部数字在 [HGamey/docs](https://github.com/HGamey/docs) 的 `experiments/2026-10-24-selfsr/`。
`HARNESS_GUIDE.md` 讲每个数字怎么来的，`STEP1–6.md` 是按步骤的实验记录，`VENDOR_REPORT.md` 是对外汇总。
这些文档里的路径还写着 phonefarm `loop_v1/selfsr/`、`knobs/gray/`，现在分别对应本仓库的 `selfsr/` 和 `selfsr/gray/`。

## 1. 对 9f7ff13 的检查

结论：没改坏。`PF_ROOT` 回退、`HERE` 找同目录文件、`DEVLOCK_LABEL` 前缀和 `PF_JOB_OUT` 都对，层源码和 pf2 最新版逐文件一致。
补了一个提交 `cb6d8fe`，内容是：

- 用法注释里残留的 `loop_v1/...` 改成本仓库路径；
- `repro_noroot.sh` 缺省输出目录原来是当前目录，改成和其他入口一样进 `PF_JOB_OUT`。

在节点上跑之前要注意三点：

1. **NDK。** `repro.sh` 每次现编层。`gray/build/build_layer.sh` 按以下顺序找 NDK r28c：
   - `REFBENCH_NDK`；
   - brew 的 `ndk/28.2.13676358`；
   - `ANDROID_NDK_HOME`；
   - `~/Library/Android/sdk/ndk/28.2.13676358`；
   - 相对层目录往上四五级的 `tools/ndk`。

   第五项是 pf2 老布局用的，在任务输入目录里找不到。mbp1 节点上 NDK 在 `/Users/mac/hgamey/tools/ndk/android-ndk-r28c`，节点配置里要给 `ANDROID_NDK_HOME`，否则编层失败，整轮退出。
2. **任务输入目录要可写。** 编层产物写在 `selfsr/gray/build/out/`。如果节点把提交解成只读，就要改成把 `out/` 指到 `PF_JOB_OUT`。我这边没法验证，请确认一下。
3. **要用 `--device-access write`。** `repro.sh` 要 root，并且会改很多设备设置（见第 6 节）。README 里的示例命令没带这个参数，selfsr 一定要带。
   - 设备锁由脚本自己拿：不挂层的臂走 `tools/devlock run <DEVLOCK_LABEL>-selfsr-...@<serial>`；挂层的臂由 `phonefarm gray-layer session` 拿租约，标签继承环境里的 `DEVLOCK_LABEL`（phonefarm `devlock.rs`），也能对到任务。
   - 如果节点在 `--device-access write` 下先替任务占了这台手机，并且给子进程带了 `DEVLOCK_INSTANCE_ID`，就要确认脚本里再拿同一台手机的锁不会排队排到超时。`locked()` 最多重试 90 次。

## 2. 目前最好的配置

用的手机是带 waitframe 和 G-FRC+ 的红魔主测机，游戏是原神，游戏内设置不改。

| 目的 | repro 组 | 臂的写法 | 结果 |
|---|---|---|---|
| **插帧（主推）** | 12（以及 4、11） | `ip:bm:120:stamp=0,panel=120,backend=sc,lead_ns=25000000,unlimit=1,sc_name=auto` | 上屏 89.7 → **118.7**（有效 4 对，4/4 同向，p=0.125）。SF 丢帧 1204 → 4.5 / 20 s。游戏渲染、GPU、延迟不变。不要 root 改系统设置（`step4/name_auto_ab`） |
| 插帧本身（对照也挂层、也放开限帧） | 4 | 同上（旧做法 `sfwait=off`） | 60.0 → 118.2（+97%，6 对，p=0.031）。真实帧延迟 +33.5 ms。整机 +1.76 W |
| 超分 | 2 / 6 | `sr:1:<屏幕宽x高>:1.0`（原神）；Megacity 锐化用 0 | HUD 区锐度约翻倍，和 G-FRC+ 只超分同档，丢帧更少（STEP3/5） |
| 超分 + 插帧 | 7 / 9 | `srip:1:<屏幕>:1.0:bm:120:stamp=0,panel=120,backend=sc,lead_ns=25000000` 再加 `,unlimit=1,sc_name=auto` | 图标区锐度 948 vs G-FRC+ 803（p=0.031）。帧率数字是关 waitframe 时测的，中位数 118.5。**改名做法下还没跑正式 A/B**，下一步应该补跑第 9 组 |

对应的属性（`selfsr_ab.py` 的 `prepare`）：

- `debug.knobs.interp=1`
- `debug.knobs.interp_mode=bm`
- `debug.knobs.interp_fps=120`
- `debug.knobs.interp_stamp=0`
- `debug.knobs.interp_backend=sc`
- `debug.knobs.interp_lead_ns=25000000`
- `debug.knobs.interp_sc_name=auto`（层缺省就是 auto）
- `debug.knobs.interp_static` 留空。层缺省值 32 是静止保护，HUD 重影 −35%，见第 11 组。
- `debug.knobs.unlimit=1`
- 超分另设 `debug.knobs.selfsr=<宽>x<高>:<锐化>`。

此外，进大世界之后要把 `settings system refresh_rate_mode` 设为 3（120 Hz）。这不是层属性，脚本会在进世界后设。

一键跑最有价值的几组：`SETS="12 11 9" sh selfsr/repro.sh <serial> 6`。

## 3. 报告是不是最新结论

是。`experiments/2026-10-24-selfsr` 的最新提交是 d94a363（2026-10-28）。STEP4 末尾、`VENDOR_REPORT.md` 3.1 节和 `REDMAGIC_SF_ASK.md` 顶部都已经写到"改名绕开 waitframe"。

只有一个待查项：部分臂在测量窗口结束后，屏幕被系统降到 60 Hz，这些臂被判作废。

- 原因已经从 trace 看出来：是厂商刷新率控制器把允许范围改成了只有 60 Hz（`primaryRangeIsSingleRate`），和层无关。
- 什么条件触发（长时间不触摸，还是温度）还不知道。
- `selfsr/refresh_probe.sh` 就是为这个写的，还没在手机上跑。

对外页面在 https://hgamey-selfsr-report.vercel.app ，源码是同目录的 `site/`。

## 4. 超分有没有自训模型

- **selfsr（本目录）没有模型。** 超分是 Catmull-Rom 放大加 RCAS 锐化，插帧是块匹配（bm），都是手写 compute shader（`gray/layer/*.comp`），没有权重。
- **NPU 超分（`npu_exp/`，N1/N3）有一个很小的自训模型。**
  - 训练代码：HGamey/docs `experiments/2026-10-20-npu-sr/train_sr.py`。模型是 ESPCN 型，2 倍超分，导出成 int8 TFLite。
  - 测速用的随机权重模型由同目录的 `make_sr.py` 生成。
  - 权重：同目录 `models/`，有 `genshin_sr_{16_16,32_32}_*.weights.h5` 和 `*_int8.tflite`，旁边的 `*_quality.json` 记了 PSNR。
  - 训练数据：原神真机截帧 `ref_*.png`。输入是把原帧做 2×2 平均下采样，目标是原帧。截帧没有入库，原始目录我手头没有可靠记录。需要的话可以用 phonefarm 截图重新采，几十张就够，模型只有几万个参数。
  - 结论见 HGamey/docs `STATUS.md` 的 10-20 一节：同一个模型，NPU 平衡档完整流程 0.28 W，GPU 跑是 0.78 W，省约 0.5 W；但完整流程要 9.6 ms，进游戏进程还要绕开 3 处系统限制。
  - selfsr 是另一条线：不用模型，用传统算子，成本更低，也能直接比画质。两条线没有合并。

## 5. 怎么跑一次完整 A/B

- **入口：** `selfsr/repro.sh <serial> [配对数=6] [输出目录] [包名]`，每组是冷启动 ABBA 加精确符号翻转检验。
  - 单组直接用 `selfsr/selfsr_ab.py run --a <臂> --b <臂> --rounds 6`。
  - 平台上：`pf-node --experiment main --research <ID> --device-access write --detach experiment selfsr/repro.sh <serial> 6`。
- **常用环境变量：**
  - `SETS`：选组；
  - `WINDOW`：测量窗口，缺省 10 s，测功耗用 30；
  - `POWER=1`：停充测整机功耗；
  - `PAN=1`：转视角测 HUD 重影；
  - `COOL_MAX`：缺省 42 °C。
- **耗时：** 每臂大约 3–5 分钟，包括等电池降到 `COOL_MAX` 以下、冷启动、自动进大世界、静置、测量窗口和收尾。6 对是 12 臂，一组约 40–60 分钟；`SETS` 全跑（1–9）要 6–8 小时。手机热的时候，主要时间花在等降温上。
- **前置条件：**
  - root（`su -c id` 返回 uid=0），层要拷进游戏的 lib 目录；不 root 的路线是 `repro_noroot.sh`，只能用可调试的 refbench 或重打包的 APK。
  - 游戏已安装并登录过，`phonefarm onboard --enter-only` 能自动进大世界。测试用的原神版本是 7.1.0。
  - 游戏内画质不改。
  - 刷新率不用手动设：脚本会在进世界后设 120 Hz，收尾写回。
  - 手机接在节点的 USB 上；要测功耗，必须是有 `/sys/class/qcom-battery/charging_enabled` 的高通机。
  - 室温正常，风扇由脚本开到最高档。
  - 第 5、6、8、9 组需要 G-FRC+（`ro.vendor.feature.zte_feature_gfrc=true`），第 12 组需要 waitframe（`ro.vendor.feature.zte_feature_fps_control_wait_frame=true`），没有的机器会自动跳过。

## 6. 会改哪些设备状态，怎么恢复

每臂结束和整轮结束（包括 Ctrl-C）时，脚本会写回原值并回读核对，核对不过就中止。原值保存在输出目录的 `orig.json` 和 `panel_orig.json`。

| 改了什么 | 怎么改的 | 手动恢复 |
|---|---|---|
| Game Mode / 降分辨率 | `cmd game mode` / `cmd game set --downscale` | `cmd game reset <包名>`，模式写回 `orig.json` 里的值 |
| G-FRC+ 本包位 | `settings global game_gfrc_mode` | 写回 `orig.json` 的 `game_gfrc_mode` |
| 层属性 | `debug.knobs.*`、`debug.vulkan.layers`（shell 身份写） | `setprop <名字> ''`，把这些属性逐个清空 |
| 层文件 | `.so` 拷进 `/data/app/…/lib/arm64` | `phonefarm gray-layer session` 结束时删除。异常退出时手动删除，或重装游戏 |
| 刷新率 | `settings system refresh_rate_mode` | 写回 `panel_orig.json` 里的值（原值为空时用 `settings delete`） |
| SF 帧率覆盖 | `service call SurfaceFlinger 1039`（只在用 `sfovr` 时） | 对同一个 uid 再下一次 0 Hz（`restore_panel` 会逐个删） |
| SF waitframe | `dumpsys SurfaceFlinger --fpsControl_waitframe_off`（只在 `SFWAIT_OFF=1` 时；现在缺省不用） | `--fpsControl_waitframe_on`。重启手机也会恢复 |
| 高通帧节奏 | `vendor.perf.framepacing.enable`（只在用 `framepacing` 时） | 写回原值 |
| 风扇 | `/sys/kernel/fan`（`--fan`） | 写回原值 |
| 屏幕超时、亮度 | `screen_off_timeout`；`--power` 时把亮度改为手动 | 写回原值 |
| 充电 | `--power` 时 `charging_enabled=0` | `echo 1 > /sys/class/qcom-battery/charging_enabled` |
| 不亮屏（noroot） | `svc power stayon usb` | `svc power stayon false` |

整轮被打断后，最简单的恢复办法是用同一个输出目录调一次收尾：

```
python3 -I selfsr/selfsr_ab.py _locked --what restore ... --out <同一目录>
```

或者对照上表手动恢复，最后重启手机。

## 7. pf2 / pf2b

- **pf2** 是我在 mbp1 上的实验工作树，从本机 rsync 过去，没有 git。代码已经全部进了 phonefarm `feat/selfsr`，现在也在本仓库。
  - 它的 `_runs/` 里是原始运行数据。报告里引用的那些已经复制到 HGamey/docs 的 `step*/` 下；只有 `_runs/name_auto_ab2`（第 12 组补的一对）还没复制。
  - 建议：先把 `_runs/` 打包，放进平台的任务产物或者对象存储，然后可以删。我会先把 `name_auto_ab2` 补进 docs。
- **pf2b 不是 A/B 基线。** 它只是第二份层的构建树：当时在两台手机上同时试不同版本的层（例如插帧合成 shader 的变体），为了不互相覆盖 `build/out/*.so` 才单独建的。A/B 的基线是同一轮里的对照臂。pf2b 可以直接删除。

## 8. 别处还有没有东西

- **代码：** 没有未提交的。phonefarm `feat/selfsr` 和本仓库一致（截至 f1d6057），HGamey/docs 已推送。
  - refbench `feat/fps-cap` 对应 refbench PR #8（未合并）。
  - phonefarm PR #69（stable 分支持续部署）也还开着，仓库归档后请决定是迁移到 HGamey/hgamey 还是关掉。
- **我本机（不是节点）：**
  - `sfpatch/`：早期 SF 探针，以及 `lease_backup_102206`；
  - `megacity_dbg/`：重打包的可调试 Megacity APK，第三方 APK 不能进公开仓库；
  - `release/`：给外部的打包 zip。

  这些都是数据或产物，不是代码。
- **云：** Vercel 项目 `hgamey-selfsr-report`，就是上面那个静态页，从 docs 的 `site/` 手动 `vercel deploy`，没有自动部署和定时任务。
- **定时任务、服务：** 我这边没有给 selfsr 起任何 launchd、cron 或常驻进程。节点上的 `com.hgamey.*`（包括 stable 持续部署）是平台本身的；PR #69 那套持续部署拉 GitHub 偶尔失败，之前记过一次。实验都是手动起、跑完就退出。
