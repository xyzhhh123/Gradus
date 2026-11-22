# 评测用的「agent 盒子」：只装 Gradus 本身，不装仓库依赖。
#
# 容器就是边界——里面的 gradus 用 `sandbox: local`（本机的受限令牌在 Linux 上不存在）。
# 仓库通过 bind mount 挂到 /workspace，跑完把 patch 与用量写到 /output。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# git 用于取 diff；ripgrep 让 grep 工具走真实的 rg；其余是模型常用的 bash 工具
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY . /gradus
WORKDIR /gradus
RUN pip install --no-cache-dir -e /gradus

WORKDIR /workspace
