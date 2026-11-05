# Doom3 实时 SmallSR 放回

现有 v73 APK 不重装：加载本地 `libdoom3.so`，游戏在半宽半高 viewport 渲染，交换屏幕前运行三段 GLES3 shader。前两段输出 4×RGBA16F 特征图，第三段做 2× depth_to_space、加 bicubic、限制到显示范围。包括游戏 HUD；Android 触摸按钮和 ImGui 在其后原尺寸绘制。

## 装机（glm 通过平台执行）

1. 目标为 **arm64-v8a、idTech4A++ v73 / 11073**。启动器选 **GLES3、MSAA=0**。将包内 `libdoom3.so` 推到**游戏运行目录**：若启用独立 Doom3 子目录，放子目录内，与 `base/` 平级；没有独立子目录则放工作目录。将 `sr_*.cfg` 推到 `base/`。
2. 启动器设置 → 调试 → **加载本地引擎库 / Load local engine library**，打开；启动 Doom3。源码对应 preference `harm_load_local_engine_lib`。这是启动器本来就有的入口，会复制库到应用私有缓存；不用改 APK、root 或签名。
3. 控制台执行 `exec sr_off.cfg` / `exec sr_on.cfg`，即同一低分辨率的模型关/开；`exec sr_native.cfg` 是全分辨率参照。首次加载 shader 后等数秒再测帧率。

快捷切换：执行一次 `exec sr_keys.cfg`；F8 在模型关/开间切，F9 回原生。可把启动器现有虚拟按键配置成 F8。也可直接执行 `r_smallSR 1` / `r_smallSR 2`；模式 0 是原生。

日志必须出现 `Load local engine library` 和 `SmallSR buffers W×H -> 2W×2H ... weights SHA`。前者只证明库被加载，后者才证明走到推理入口。`r_smallSR` 默认 0；不要把默认原生画面当作模型结果。截图用平台的屏幕截图/adb screencap；引擎自己的截图可能发生在最后的放大之前。

退出游戏、关闭“加载本地引擎库”即可回官方引擎。模型开关不需要重启；替换 `.so` 或权重版本需要退出重开。

## 构建与验证

上游 `https://github.com/glKarin/com.n0n3m4.diii4a`，tag `v1.1.0harmattan73`，commit `ce49510f3b0c04969eab8ebe8238416e84e4f0bb`。Android NDK r23c（23.2.8568313），CMake，Python/NumPy。脚本在干净的上游 checkout 上执行：

```sh
bash build_engine.sh /path/to/v73 /path/to/android-ndk-r23c weights.npz /path/to/output
python3 validate_gles.py weights.npz /path/to/output/shaders
```

第二条需要 PyOpenGL、真实 EGL/GLES3 context；它将实际 shader 输出与 NumPy 三层卷积逐层比对，含上下方向、零填充、像素重排。宿主验证不能证明手机 fps。

权重是 NHWC `w1[5,5,3,16], b1[16], w2[3,3,16,16], b2[16], w3[3,3,16,12], b3[12]`。`export_glsl.py` 生成源码和 SHA/形状清单。换权重只需重新生成头文件、复制到已打补丁引擎的 `doom3/neo/renderer/rb/` 并增量构建；不用再次执行 patch_engine.py。

双三次为 a=-0.5、半像素中心、边缘截断后归一的浮点实现；没有 Pillow 的中间 8-bit 取整。真实训练权重到达后须核对其预处理并重跑数值验证。中间特征用 FP16，输入为屏幕 RGB8；不进行额外线性化或归一化。

## 验收边界

先确认库加载、三模式和开关，再用同一存档/画面录屏，报告模型关/开与原生参照，以及稳定窗口的平均 fps、1% low。GPU 在手机上实际跑过才算装回。`pipeline-fixture` 包使用**未训练的合成权重**，仅用于提前验加载/GL 驱动/耗时，不能作画质交付。

代码按 GPL-3.0-or-later 提供。上游修改由 `patch_engine.py` 和 `small_sr.cpp` 完整描述，shader 可从随包 weights.npz 重建；包附上游许可证，不包含 Doom 商业游戏数据。
