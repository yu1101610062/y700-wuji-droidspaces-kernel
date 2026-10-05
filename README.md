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

开启：`SYSVIPC`、`POSIX_MQUEUE`、`IPC_NS`、`PID_NS`、`USER_NS`、`DEVTMPFS`（`DEVTMPFS_MOUNT` 保持关闭）。
不开：`CGROUP_PIDS`、`CFS_BANDWIDTH`。它们会改调度器 / cgroup 结构体，破坏 KMI。

## 构建与校验

GitHub Actions（[`build.yml`](.github/workflows/build.yml)）按固定 manifest 同步源码，用官方 Kleaf 构建 `//common:kernel_aarch64_dist`，然后运行 [`scripts/verify_build.py`](scripts/verify_build.py)，以 Google 的 build 14494108 为基准比对：

1. `.config` 只有上述选项变化，`CONFIG_LTO_NONE=y` 不变。打包进 task_struct 的字段依赖非 LTO 的普通 `READ_ONCE`。
2. `vmlinux.symvers`、`Module.symvers` 中原厂全部导出符号的 CRC、所属模块、导出类型都不变，只允许新增。
3. 内核版本字符串与原厂一致。
4. 原厂 GKI 签名证书已嵌入。
5. 新旧 Image 内嵌 BTF 中所有命名 struct/union/enum 的布局一致，只允许 `task_struct` 空洞里新增的两个成员和 `sysv_sem`/`sysv_shm` 变化。

## 使用

本仓库只产出 `Image`，不包含任何设备固件或备份。按以下步骤在本地使用：

1. 从自己设备当前槽位备份原厂 `boot`，用 `scripts/repack_boot.py` 替换内核，并保留原 AVB 属性。
2. 先 `fastboot boot new-boot.img` 临时启动验证，确认模块全部加载、Wi‑Fi/蓝牙正常、`droidspaces check` 通过后再考虑刷写。
3. `init_boot`（KernelSU LKM）无需改动。每次 OTA 都会覆盖 boot，需要按新版本的 GKI tag 重新构建。

仅适用于运行同一 GKI build（14494108）的设备。风险自负。
