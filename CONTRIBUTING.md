# 贡献指南 | Contributing Guide

感谢你对 WallWhisper 项目感兴趣！🎉 以下是参与贡献的指南。

Thank you for your interest in WallWhisper! 🎉 Here's how you can contribute.

## 🌟 如何贡献 | How to Contribute

### 报告 Bug | Report Bugs
- 使用 GitHub Issues 提交 bug 报告
- 请包含：复现步骤、预期行为、实际行为、环境信息（OS、Python 版本、设备型号）

### 功能建议 | Feature Requests
- 使用 GitHub Issues 提交功能建议
- 描述你的使用场景和预期效果

### 提交代码 | Pull Requests
1. Fork 本仓库
2. 创建特性分支：`git checkout -b feat/your-feature`
3. 提交更改：`git commit -m "feat: add your feature"`
4. 推送分支：`git push origin feat/your-feature`
5. 创建 Pull Request

## 📐 代码规范 | Code Style

- Python 代码遵循 PEP 8
- 所有文件读写显式指定 `encoding='utf-8'`
- 使用 Python `logging` 模块，格式：`%(asctime)s [%(levelname)s] %(message)s`
- 密钥/凭证不要硬编码，全部从 `config.yaml` 读取
- 提交信息使用英文，格式：`类型: 简要描述`
  - `feat:` 新功能
  - `fix:` 修复
  - `docs:` 文档
  - `refactor:` 重构
  - `test:` 测试

## 🔒 安全须知 | Security

- **绝对不要** 在代码或 PR 中包含 API 密钥、Token 等敏感信息
- 配置文件 (`config.yaml`, `config.docker.yaml`) 已被 `.gitignore` 排除
- 如果发现安全问题，请私信联系维护者而非公开 Issue

## 🎯 贡献方向 | Areas for Contribution

- 🌍 支持更多 TTS 服务（Azure TTS、Google TTS 等）
- 📹 支持更多摄像头品牌（海康、大华等）
- 🧠 支持更多 AI 模型（Qwen、ChatGLM 等）
- 🏠 支持更多智能家居平台（HomeAssistant 等）
- 📚 优化英语教学内容和策略
- 🌐 支持更多语言的教学
- 📖 改进文档和教程

## 💬 交流 | Community

- GitHub Issues：问题反馈和功能讨论
- GitHub Discussions：通用讨论和经验分享

感谢每一位贡献者！❤️
