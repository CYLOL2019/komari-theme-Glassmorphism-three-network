# Komari Declarative Config Sync (`komari-sync`)

将 Komari 的全部服务器配置（节点名称、地区、分组、标签、月费/续费周期/到期时间、流量限额、备注、三网 Ping 任务、站点基础设置与主题配置）保存为一份可读、可版本管理的 **YAML 或 JSON** 文件，后续迁移服务器或重装面板时 **一条命令即可幂等导入**，不再需要在管理后台逐台手点。

## 核心特点

- **同时支持 YAML 与 JSON**：`.json` 零第三方依赖（纯 Python 3 标准库）；`.yml` / `.yaml` 自动调用 `PyYAML`。
- **Ping 任务按节点名绑定，不依赖 UUID**：导出时自动把节点 UUID 翻译成节点名；导入新面板时自动创建节点、获取新 UUID 并重新绑定所有 Ping 任务。
- **人类可读流量配额**：支持 `200 GiB`、`2 TiB`、`500 GB` 等写法，也兼容纯字节整数。
- **默认零隐私泄露**：`export` 默认不导出 `uuid` 与 `token`、也不导出密码/API Key，可安全分享模板；私人备份加 `--include-secrets` 会自动设 `chmod 600`。
- **幂等更新 & 预演对比**：支持 `diff` / `apply --dry-run` 预览差异，重复执行只修改真正变化了的字段；新建节点后可搭配 `--emit-agents` 直接打印开箱即用的 `docker run komari-agent` 命令。

## 快速上手

为避免密码留在命令行历史中，推荐先用环境变量注入：

```bash
export KOMARI_URL="http://127.0.0.1:25774"
export KOMARI_USERNAME="admin"
export KOMARI_PASSWORD="your-admin-password"
```

### 1. 从现有 Komari 一键导出配置

```bash
# 导出为 YAML（默认不含 token，适合作为配置模板）
python3 komari_sync.py export -o komari-config.yml

# 或导出为 JSON（无需安装 PyYAML）
python3 komari_sync.py export -o komari-config.json

# 私人本地备份（包含节点 uuid/token，自动 chmod 600，切勿提交到公开仓库）
python3 komari_sync.py export --include-secrets -o komari-backup-private.yml
```

### 2. 对比差异（不改动服务端）

```bash
python3 komari_sync.py diff -f komari-config.yml
```

### 3. 一键导入 / 同步到新服务器或现网面板

```bash
# 幂等同步：缺失节点自动新建，已有节点按字段增量更新，Ping 任务与主题设置一并同步
python3 komari_sync.py apply -f komari-config.yml

# 同步并打印每台节点的 Docker komari-agent 启动命令
python3 komari_sync.py apply -f komari-config.yml --emit-agents https://status.example.com

# 只同步某几类配置（可选：site, nodes, ping, theme）
python3 komari_sync.py apply -f komari-config.yml --only nodes,ping
```

### 4. 联动大陆连通性探针 (`mainland-probe`)

如果在 YAML/JSON 中同时维护了 `mainland_probe.targets`（见 [`example-config.yml`](example-config.yml)），可直接渲染 `PROBE_TARGETS` 环境变量：

```bash
python3 komari_sync.py render-probe -f komari-config.yml
```
