# Komari Glassmorphism 三网延迟 + 大陆连通性检测版

![Komari Glassmorphism 预览](docs/preview.png)

本项目基于 [vlongx/komari-theme-Glassmorphism-three-network](https://github.com/vlongx/komari-theme-Glassmorphism-three-network) 与上游 [sanrokamlan-prog/komari-theme-Glassmorphism](https://github.com/sanrokamlan-prog/komari-theme-Glassmorphism) 二次开发，遵循原项目 **MIT License**。

## 新增功能（v2.5.x）

1. **中国大陆连通性 / 封锁检测徽标（GFW / TCP & ICMP）**
   - 在节点卡片与列表视图中自动展示每个节点的中国大陆直连状态：
     - 🟢 **大陆直连**：TCP 端口从中国大陆直连可达
     - 🟠 **TCP阻断**：TCP 端口超时/阻断，但 ICMP 仍可通
     - 🔴 **疑似被墙**：TCP 与 ICMP 均不可达
   - 悬停徽标可查看探测点、端口、TCP/ICMP 延迟与最近检测时间；未配置检测的节点保持界面整洁、不显示多余标签。
   - **支持两种零隐私硬编码的接入方式**（均在 Komari 后台 → 主题设置配置）：
     - **方式 A（Komari 原生 Ping 任务）**：在位于中国大陆的探测节点上创建 Ping/TCPing 任务，按 `大陆TCP:<目标节点名>` / `大陆ICMP:<目标节点名>`（或 `CN-TCP:<节点名>` / `GFW-TCP:<节点名>`）命名，主题自动复用现有 RPC 缓存并归类展示，无需额外服务。
     - **方式 B（独立探针 JSON）**：在后台填写 **外部探针 JSON 地址**（如 `/mainland-status.json`），可搭配仓库自带的零依赖探针脚本 [`scripts/mainland-probe/probe.py`](scripts/mainland-probe/probe.py) 部署到中国大陆主机（支持 `--interface` 指定物理网卡 `SO_BINDTODEVICE`，即使宿主机开着 Clash / mihomo TUN 也能测出真实大陆直连状态）。
2. **节点卡片 GPU 实时占用条**
   - 当节点上报了有效 `gpu_name` 或 `gpu_detailed_info`（如 `komari-agent --gpu`）时，自动在首页节点卡片展示 GPU 型号、显存、温度与实时利用率进度条；无 GPU 节点自动隐藏。
3. **声明式 YAML / JSON 面板配置导入导出工具（`komari-sync`）**
   - 仓库内置 [`scripts/komari-sync/komari_sync.py`](scripts/komari-sync/komari_sync.py)（详见 [`scripts/komari-sync/README.md`](scripts/komari-sync/README.md)），支持将 Komari 的节点列表（地区/分组/标签/月费/账单周期/流量配额/备注）、三网 Ping 任务（按节点名绑定，不依赖 UUID）、站点设置与主题配置一键导出为 YAML 或 JSON，迁移或重装面板时一条命令 `apply` 即可幂等还原。
4. **完整保留原版全部特性**
   - 联通 / 电信 / 移动三网独立延迟与丢包柱状图、玻璃拟态 UI、3D 地球、月费/剩余价值统计、流量配额进度条、浅色/深色/北京时间自动切换。

## 安装方式

1. 从本仓库 **Releases** 下载编译好的 `komari-theme-Glassmorphism-build-*.zip`（不要下载 GitHub 自动打包的 `Source code`）。
2. 进入 Komari 管理后台 → **主题管理** → 上传 ZIP 并启用。
3. 如需开启外部大陆探针，进入 **主题设置 → 中国大陆连通性检测** 填写探针 JSON 地址（详见 [`scripts/mainland-probe/README.md`](scripts/mainland-probe/README.md)）。

## 本地开发与构建

```bash
bun install
bun run lint
bun run build
```

构建完成后会在根目录生成可直接导入 Komari 的 `komari-theme-Glassmorphism-build-<sha>.zip`。

## 致谢

感谢上游作者 **sanrokamlan-prog** 与 **vlongx** 的开源贡献。
