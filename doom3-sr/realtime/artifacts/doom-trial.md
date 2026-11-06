# Doom 试跑模型包

`small-sr-doom-arm64.tar.gz`，arm64-v8a / GLES3 / MSAA=0，idTech4A++ v73。
训练变体 doom_scratch，5,276 参数；来源作业 3aa25dd077c5，训练代码 kimi/qrisp-sr 的 cdb6c04。
250 帧几乎都是同一走廊；训练方报告较 bicubic +2.64 dB，仅限该试跑划分，不能外推。这里交付用于真机开/关体验与帧率验收。

- NPZ SHA256：6cc44b53ab9d6701617c48a24515a49ac20ca7e7417bcb3c7d5d02570f1d60e5
- 包 SHA256：3a2243f84e20c6e7358a7adf627567ee633c1611bc14055bed6f9a9448dd6b8f
- libdoom3.so SHA256：e3314b7357e6d79973a5b847c1ec3c680b133e01d9bc387276669d3c43107fe6

退出游戏，将 `.so` 放入启动器实际游戏工作目录（与 `base/` 平级），配置文件放入 `base/`；启用“Load local engine library”，重新启动。完整步骤见上级 README 和包内 README。
`exec sr_off.cfg` 为半分辨率+bicubic，`exec sr_on.cfg` 为半分辨率+真实模型，`exec sr_native.cfg` 为原生参照。`exec sr_keys.cfg` 绑定 F8 切模型开/关，F9 回原生。
验收日志须包含 SmallSR buffers 和上述 NPZ 哈希，防止继续加载旧占位库。

本机实际 GLES 3.2 验证通过，模型 float 输出对 NumPy 最大误差 0.0004593；实际 C++ RGB8 输出最大误差 0.0022823，模型关闭与 Pillow 逐字节相等；中间层、像素重排和开/关切换通过，未改任何阈值。
包内 27 个文件的哈希和长度均已核对，ELF64 AArch64，二进制含正确权重哈希。手机加载、画质和帧率待 D 验收。
