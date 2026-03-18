<div align="center">

# 🤫 WallWhisper — The Whisper on Your Wall

### Turn your existing camera and router into a zero-cost AI English tutor

**OpenClaw × EZVIZ Camera × Router = Kids walk by, English starts playing**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-green.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ARM64%20%7C%20x86__64-blue.svg)](Dockerfile)

English | [简体中文](README.md)

</div>

---

## ✨ What is WallWhisper?

> *Your daughter comes home from kindergarten and walks into the living room—*
>
> *The camera on the wall quietly stirs.*
>
> *"Hello! Cat! Can you say cat? Meow!"*
>
> *She giggles: "Cat! Meow meow!"*
>
> *This is WallWhisper — the whisper on your wall, an English friend named Emily who lives inside your camera.*

**WallWhisper** is an open-source home English teaching system. Its core persona **Emily** uses EZVIZ cameras to detect family members, automatically generates personalized English content, and plays it through the camera's built-in speaker.

No extra hardware. No app to open. No need to "start a lesson" — **English learning happens naturally as your child moves around the house.**

## 🔥 Why WallWhisper?

### 💡 Core Philosophy: Language learning needs an environment, not motivation

As a programmer dad, I've been thinking about how to naturally expose my 3-year-old to English. **The most critical factor in language learning isn't how good the textbook is or how skilled the teacher is — it's whether there's a persistent language environment.**

Yet every English learning tool on the market has the same fatal flaw — **they all require the child to "come to them":**

| Approach | Problem |
|----------|---------|
| 📱 Phone/Tablet Apps | Require the child to actively open them — a 3-year-old can't do that |
| 📺 English TV Shows | Require channel switching, content selection — plus screen addiction risk |
| 📚 English Books | Require parents to co-read — limited time and energy |
| 👩‍🏫 Private Tutors | Twice a week, forgotten in between, $3000+/year |
| 🤖 AI Chat Products | Require kids to sit in front of a screen and actively interact |

**All of these test a child's "learning initiative." But for a 3-year-old, initiative itself is a luxury.**

So I flipped the approach: **Instead of making the child go to English, let English come to the child.**

### 🏠 Growing a "Whisper on Your Wall"

To create an English environment, nothing beats having a **hidden English tutor built into your home**. The question is: who plays that role?

The answer is already on your wall — your camera. That's where the name **WallWhisper** comes from: **a whisper from the wall.**

**📷 Cameras are the perfect natural carrier:**
- 🫥 **Non-intrusive** — It's already on your wall/ceiling. Your child is used to it being there
- 👀 **Wide field of view** — Much better at capturing a child's movement than a phone or tablet
- 🔊 **Built-in speaker** — No extra Bluetooth speaker needed. One device handles both sensing and playback
- 🧒 **Zero operation required** — No buttons to press. Walk by and it triggers. True zero-interaction

**🧠 OpenClaw gives Emily a soul:**

Traditional English players can only mechanically repeat pre-recorded audio. Emily is different — built on the [OpenClaw](https://openclaw.com) open-source AI Agent platform, Emily has a complete **personality, memory, and growth system**:

- She knows your child's name, age, and interests
- She remembers which words she taught last week and what to review today
- She gradually increases difficulty based on your child's level
- **She grows alongside your child**, rather than being stuck on "Hello, how are you?" forever

**📦 Your router is Emily's natural home:**

Emily needs a 24/7 host to run on. Instead of buying a new server or Raspberry Pi, look at what's already in your home — **your router**:

- 🔌 Already running 24/7 — no extra electricity cost
- 🌐 Already the home gateway — zero-latency LAN access to the camera
- 🐳 Docker-capable routers (Xiaomi 7000/BE series) run the WallWhisper container directly
- 💰 **Zero new hardware cost** — efficient and economical

> **In summary: Camera senses the child + OpenClaw generates personalized English + Router runs 24/7 = WallWhisper, the whisper on your wall. All using existing devices, with zero additional cost.**

## 🎯 Key Features

| 🏠 Immersive English Environment | 🧠 OpenClaw — Grows with Your Child | 🔊 Camera Speaker Output | 💰 Zero Extra Hardware Cost |
|:---:|:---:|:---:|:---:|
| No need for the child to "open" anything — walk by and English comes to them | Emily "knows" your family, remembers vocabulary taught, adjusts difficulty over time | RTSP Backchannel pushes audio directly to the camera speaker — no extra speaker needed | Camera + Router + AI API — all repurposed from existing home devices |

## 🚀 Quick Start

```bash
# Clone
git clone https://github.com/treychen-369/WallWhisper.git
cd WallWhisper

# Install
pip install -r requirements.txt

# Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your API keys

# Test TTS
python run.py test_tts "Hello! Can you say cat?"

# Run Emily
python run.py emily
```

For Docker deployment and router setup, see [Deployment Guide](docs/deployment-guide.md).

## 🎬 Three Interaction Modes

| Mode | Trigger | Content | Example |
|:---:|:---:|:---:|:---|
| 🚶 **pass_by** | Person walks past camera | Quick greeting + one word | *"Hello! Cat! Can you say cat? Meow!"* |
| 🧑‍🏫 **interact** | Multiple triggers in short time | Deep interactive teaching | *"Hi! Look, a dog! Woof woof! Can you say dog?"* |
| ⏰ **scheduled** | Timed tasks | Morning briefing / bedtime story | *"Good morning! Let's learn a color today! Red!"* |

Every English segment is followed by a Chinese explanation (bilingual output), ensuring the child understands.

## 🤝 Contributing

Issues and Pull Requests welcome! See [Contributing Guide](CONTRIBUTING.md).

## 📄 License

[MIT License](LICENSE)

---

<div align="center">

**If WallWhisper helps your family, please give us a ⭐ Star!**

*Made with ❤️ by a programmer dad who wants his daughter to love English.*

*WallWhisper — The whisper on your wall, letting English surround your child like air.*

</div>
