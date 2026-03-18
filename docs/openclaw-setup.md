# OpenClaw Emily 人设配置指南

## 什么是 OpenClaw？

[OpenClaw](https://openclaw.com) 是一个开源 AI Agent 平台。WallWhisper 基于 OpenClaw 构建了完整的人设和记忆系统，使 Emily 能够：

- 🧠 **记住**教过的单词，避免重复
- 👨‍👩‍👧 **认识**你的家人，根据每个人的水平调整难度
- 💝 **成长**——随着时间推移，Emily 的教学会越来越精准

> 💡 **不使用 OpenClaw 也能运行 WallWhisper！** 不配置 OpenClaw 时，Emily 会直接调用 DeepSeek API，每次生成独立内容。OpenClaw 提供的是人设一致性和长期记忆。

## 配置步骤

### 1. 部署 OpenClaw

参考 [OpenClaw 官方文档](https://openclaw.com/docs) 在你的服务器上部署 OpenClaw。

推荐使用腾讯云轻量应用服务器（Lighthouse），2C2G 即可。

### 2. 创建 Emily Agent

在 OpenClaw 中创建一个名为 `emily` 的 Agent。

### 3. 上传人设文件

将 `examples/openclaw-config/` 目录下的文件上传到 OpenClaw：

| 文件 | 上传位置 | 说明 |
|------|----------|------|
| `SOUL.md` | Emily Agent 的人设配置 | Emily 的人格、教学策略、说话方式 |
| `USER.md` | Emily Agent 的用户配置 | 你的家庭信息（⚠️ 需要自定义！） |
| `TOOLS.md` | Emily Agent 的工具配置 | 硬件环境信息 |
| `MEMORY.md` | Emily Agent 的记忆文件 | 教学进度（Emily 会自动更新） |
| `HEARTBEAT.md` | Emily Agent 的心跳配置 | 定期维护任务 |

### 4. 自定义 USER.md（重要！）

`USER.md` 是 Emily 了解你家庭的唯一途径。请务必根据实际情况修改：

```markdown
## Your Child
- **Name:** YourKidName        # ← 替换为你孩子的名字
- **Age:** 3 years old         # ← 替换为孩子年龄
- **Interests:** Frozen, Elsa  # ← 替换为孩子的兴趣爱好
```

Emily 会根据这些信息个性化教学内容。比如：
- 如果孩子喜欢 Frozen → Emily 会经常用 Elsa 举例
- 如果孩子 5 岁 → Emily 会教稍微复杂一些的词汇

### 5. 自定义 TOOLS.md

更新你的硬件环境信息：

```markdown
### Camera & Speaker
- **Camera IP:** 192.168.1.100   # ← 你的摄像头 IP
```

### 6. 部署 emily-api.py

`emily-api.py` 是一个轻量 HTTP 网关，部署在 OpenClaw 同一台服务器上：

```bash
# 上传到服务器
scp examples/openclaw-config/emily-api.py your-server:/root/

# 设置环境变量（可选）
export EMILY_API_PORT=8901
export EMILY_API_TOKEN="your-secure-token"

# 启动
nohup python3 /root/emily-api.py > /root/emily-api.log 2>&1 &

# 验证
curl http://your-server:8901/api/emily/health
# 应返回: {"status": "ok"}
```

### 7. 配置 Emily 连接 OpenClaw

在 `config.yaml` 中启用 OpenClaw：

```yaml
openclaw_emily:
  enabled: true
  api_url: "http://your-server:8901/api/emily/speak"
  api_token: "your-secure-token"  # 与 emily-api.py 的 EMILY_API_TOKEN 一致
  timeout: 30
```

### 8. 验证

```bash
# 测试端到端
python run.py test_full

# 查看日志，应该看到：
# [INFO] [Emily] 🧠 AI 模式: OpenClaw Emily API (统一人设 + 记忆)
```

## 使用 sync_openclaw.py 同步人设

修改人设文件后，使用 `sync_openclaw.py` 一键同步：

```bash
# 查看差异
python sync_openclaw.py --dry-run

# 同步有变化的文件
python sync_openclaw.py

# 查看指定文件差异
python sync_openclaw.py --diff SOUL.md

# 强制全部重传
python sync_openclaw.py --force

# 指定服务器
python sync_openclaw.py --host your-server-ip
```

## 人设文件详解

### SOUL.md — Emily 的灵魂

定义了 Emily 的人格、说话方式和教学策略：

- **人格**：温柔、耐心、爱玩
- **词汇等级**：限制在幼儿级别（单音节词为主）
- **说话格式**：中英双语（英文在前，中文解释在后）
- **交互模式**：pass_by（短）、interact（中）、scheduled（长）
- **教学策略**：一次一个词、连接孩子的世界、重复、鼓励

> 💡 你可以根据需要调整 SOUL.md，比如改为面向成人的英语教学。

### USER.md — 家庭画像

告诉 Emily 你家人的信息，用于个性化：

- 孩子的年龄、英文水平、兴趣爱好
- 家长的基本信息
- 家庭作息时间

### MEMORY.md — 教学记忆

Emily 会自动更新这个文件：

- 教过哪些单词
- 每个家庭成员的学习进度
- 观察笔记
- 里程碑

> 📌 首次使用时，MEMORY.md 是空的模板。随着 Emily 和家人互动，她会自动填充内容。

## 常见问题

**Q: 不用 OpenClaw 可以吗？**

可以！在 `config.yaml` 中设置 `openclaw_emily.enabled: false`，Emily 会直接用 DeepSeek API。只是没有长期记忆和统一人设。

**Q: OpenClaw 需要什么服务器配置？**

最低 2C2G 即可。腾讯云轻量应用服务器、阿里云 ECS、AWS EC2 等都可以。

**Q: emily-api.py 的安全性？**

- 支持 Bearer Token 认证
- Token 自动生成并保存到 `~/.emily-api-token`
- 建议使用 HTTPS（通过 Nginx 反代）
