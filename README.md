# Y700 无极 (TB324ZC) Droidspaces GKI 内核

为 Lenovo Legion Y700 无极（TB324ZC，Snapdragon SM8850 `canoe`）重新编译原厂所用的 Google GKI 内核，补上 [Droidspaces](https://github.com/ravindu644/Droidspaces-OSS) 需要的容器能力，同时保持与原厂 527 个厂商 / system_dlkm 模块的 KMI 兼容。

## 基线

| 项目 | 值 |
|---|---|
| 原厂内核 | `6.12.38-android16-5-g74ad46052215-ab14494108-4k` |
| 源码 | `android16-6.12-2025-09_r15`（kernel/common `74ad4605221549423a507537bdd3503ee11d38e2`） |
| Manifest | [`manifest/manifest_14494108.xml`](manifest/manifest_14494108.xml)，来自 ci.android.com 的 build 14494108 |
| 工具链 | 由 manifest 固定：clang r536225、rustc 1.82.0 |

联想 boot 分区里的 Image 与 Google 公开的 GKI Image 逐字节相同（SHA-256 `38c7a5ad…a392`）。

## 改动

| 补丁 | 内容 |
|---|---|
| `0001` | `sysvsem`/`sysvshm` 放进 `task_struct` 中 `rt_priority`（结束于 164）与 64 字节对齐的 `se`（192）之间 28 字节空洞，使用 `__packed` + `__kabi_ignored`，任何原有成员偏移不变，gendwarfksyms CRC 不变；对 bindgen 隐藏，原厂 `rust_binder.ko` 的 Rust KMI 不变。思路来自 Droidspaces-OSS 的 GKI 6.12 补丁和 [droidspaces-xiaomi-17-kernel](https://github.com/2824799/droidspaces-xiaomi-17-kernel)。 |
| `0002` | 导出 `init_ipc_ns`、`put_ipc_ns`，供开启 IPC_NS 后编译期的 in-tree rust_binder 链接。 |
| `0003` | `droidspaces_defconfig` 片段通过 `--defconfig_fragment` 叠加（不改 `gki_defconfig`，`minimized` 检查照常通过）；把原厂 GKI 模块签名证书加入 `CONFIG_SYSTEM_TRUSTED_KEYS`，原厂 system_dlkm 模块继续视为已签名；`workspace_status.json` 固定 SCMVERSION，使 uname 与原厂一致。 |
| `0004` | 开启 USER_NS 后 `from_kuid()` 不再是内联函数，`rust_helper_from_kuid` 会被条件编译掉，但原厂 `rust_binder.ko` 仍导入它（CRC `0x143d1b29`）。补丁让它留在原编译单元中（只对 bindgen 隐藏），并加入 `symbols/lenovo` 以免被裁剪。 |

开启：`SYSVIPC`、`POSIX_MQUEUE`、`IPC_NS`、`PID_NS`、`USER_NS`、`DEVTMPFS`（`DEVTMPFS_MOUNT` 保持关闭）。
不开：`CGROUP_PIDS`、`CFS_BANDWIDTH`。它们会改调度器 / cgroup 结构体，破坏 KMI。

## 原厂 rust_binder.ko 与 IPC_NS

原厂 system_dlkm 中的 `rust_binder.ko` 在关闭 IPC_NS 的配置下编译，其 binderfs 对 `ipc_ns` 不持有引用，但这个指针在原厂编译产物中从不解引用。此外，在本机上 Rust Binder 只在 `binder.impl=rust` 时接管，设备未设置这个参数，模块加载后在 init 中直接返回。实际使用的是内置在 Image 里的 C binder，它随本内核一起以 IPC_NS=y 编译，因此无需额外处理。`0002` 导出的两个符号只用于让本次构建里的 in-tree rust_binder 通过链接。

## 构建与校验

GitHub Actions（[`build.yml`](.github/workflows/build.yml)）按固定 manifest 同步源码，用官方 Kleaf 构建 `//common:kernel_aarch64_dist`，然后运行 [`scripts/verify_build.py`](scripts/verify_build.py)，以 Google 的 build 14494108 为基准比对：

1. `.config` 只有上述选项变化，`CONFIG_LTO_NONE=y` 不变。打包进 task_struct 的字段依赖非 LTO 的普通 `READ_ONCE`。
2. `vmlinux.symvers`、`Module.symvers` 中原厂全部导出符号的 CRC、所属模块、导出类型都不变，只允许新增。
3. 内核版本字符串与原厂一致。
4. 原厂 GKI 签名证书已嵌入。
5. 新旧 Image 内嵌 BTF 中所有命名 struct/union/enum 的布局一致，只允许 `task_struct` 空洞里新增的两个成员和 `sysv_sem`/`sysv_shm` 变化。

## 使用

[Releases](../../releases) 提供原始内核 `Image`，以及针对 TB324ZC ZUXOS 2.0.10.262（B 槽）和 2.0.10.223（A 槽）重新打包的可刷入 boot 镜像。仓库本身不包含设备固件或分区备份。

> **v1 的 boot 镜像不要用。** v1 重写了 AVB footer，算法为 NONE。在伪回锁设备上，bootloader 报告的是 `verifiedbootstate=green`，一阶段 init（`libfs_avb`）因此以锁定模式校验链式 `boot` 分区的 vbmeta 签名，校验失败后无限重启。`fastboot boot` 不会暴露这个问题，因为它读取的是磁盘上原厂签名的 boot。
>
> v2 起，`repack_boot.py` 默认使用 `--avb keep`：原样保留 OEM 签名的 vbmeta，只把它移到新内容之后，并重写不受签名保护的 footer。这和 KernelSU / Magisk 修补 boot 的方式相同。这样签名依然有效，`androidboot.vbmeta.digest` 与原厂一致；只有内容哈希不匹配，而真实解锁的 ABL 会容忍这一点。

其他固件版本请按以下步骤自行打包：

1. 从设备拉取全部原厂模块（vendor_boot ramdisk、vendor_dlkm、system_dlkm，以及 init_boot 中的 `kernelsu.ko`），用 `scripts/audit_modules.py` 核对每个导入符号的 CRC 和 vermagic。
2. 从自己设备的每个槽位备份原厂 `boot`，用 `scripts/repack_boot.py`（默认 `--avb keep`）替换内核。每个槽位要用各自的原厂 boot 打包，因为 fingerprint 不同。
3. 先 `fastboot boot new-boot.img` 临时启动验证，确认模块全部加载、Wi‑Fi/蓝牙正常、`droidspaces check` 通过后再考虑刷写。
4. `init_boot`（KernelSU LKM）无需改动。每次 OTA 都会覆盖 boot，需要按新版本的 GKI tag 重新构建。

仅适用于运行同一 GKI build（14494108）的设备。风险自负。
