# HGamey experiments

研究步骤的代码：每个目录是一类实验的脚本（和实验版本的层）。HGamey 平台按提交号在节点上运行它们；平台本身在 [HGamey/hgamey](https://github.com/HGamey/hgamey)。

## 目录

- `selfsr/`：自研超分与插帧 A/B（BH3GEI）。`gray/` 是实验版本的灰档层，`repro.sh` 现编现用。报告在 [HGamey/docs](https://github.com/HGamey/docs) 的 `experiments/2026-10-24-selfsr`。
- `gfrc/`：厂商 G-FRC+ 对照 A/B。
- `npu_exp/`：原神 NPU 超分三臂对照（N3，BH3GEI）。层和模型取自作者机器上的 N1 产物，在别处跑要给 `--layer-so` 和 `--model`。报告在 HGamey/docs 的 `experiments/2026-10-20-npu-sr`。
- `o2_dvfs/`、`repro/`：O2 DVFS 扫描与复现打包。`repro/pack_repro.py` 生成的脚本仍假定在 phonefarm 老位置运行。

历史来自 HGamey/hgamey 的 `phonefarm/loop_v1/` 与 `phonefarm/feat/selfsr` 分支，作者和时间保留。

## 在平台上跑

```sh
pf-node --experiment main --research <研究ID> --device-access write --detach \
  experiment selfsr/repro.sh <serial> 6 SETS=7 WINDOW=60
```

- 节点把这个提交解到任务输入目录，在那里运行入口脚本：`.py` 用 python3，`.sh` 用 bash。
- 节点给的环境：`PF_ROOT`（节点上的 phonefarm 树：`tools/devlock`、`loop_v1/tools` 等）、`PHONEFARM_BIN`、`GAME_OPT_LOOP_BIN`、`PF_JOB_OUT`（产物写这里）、`PF_JOB_RESEARCH`、`PF_EXPERIMENT_COMMIT`；节点配置了才有 `ANDROID_NDK_HOME`。
- 设备锁由脚本自己拿，标签写成 `${DEVLOCK_LABEL}-<用途>@<serial>`，看板据此把手机占用对到任务和研究。
- 任务默认不预留设备（`--device-access none`）。会改设备状态的实验用 `--device-access write`：任务结束停在 blocked，核查产物里的还原记录（如 `restore.json`）后 `pf-node release <任务> --note "核查结果"`。
- 任务只传参数不传环境变量：`repro.sh` 头部列出的变量可写成 `KEY=VALUE` 参数。

本地直接跑时设 `PF_ROOT=<phonefarm 目录>`（或 `PHONEFARM_BIN`）。

## 规则

- 只放代码。运行数据、截图、模型和报告不进仓库：数据随任务产物留在平台，报告写在 HGamey/docs。
- 一个提交只做一件事；commit message 写改了什么、为什么、怎么验证的。
