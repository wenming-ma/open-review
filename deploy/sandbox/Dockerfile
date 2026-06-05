FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        bear \
        ca-certificates \
        clang \
        clangd \
        clang-tidy \
        cmake \
        cppcheck \
        cscope \
        curl \
        git \
        global \
        libbenchmark-dev \
        libboost-locale-dev \
        libcairo2-dev \
        libfontconfig1-dev \
        libfmt-dev \
        libfreetype-dev \
        libgl1-mesa-dev \
        libglm-dev \
        libharfbuzz-dev \
        libpixman-1-dev \
        jq \
        nodejs \
        nlohmann-json3-dev \
        ninja-build \
        npm \
        python3 \
        ripgrep \
        shellcheck \
        universal-ctags \
    && rm -rf /var/lib/apt/lists/* 

RUN pip install --default-timeout=180 --retries=10 uv

WORKDIR /workspace

CMD ["bash", "-lc", "mkdir -p /workspace && sleep infinity"]
