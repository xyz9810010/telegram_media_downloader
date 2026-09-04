FROM python:3.11.9-alpine AS build

# 使用国内镜像源加速依赖下载（可通过 ARG 覆盖）
ARG APK_MIRROR=https://mirrors.aliyun.com/alpine
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Switch apk repos to domestic mirror
RUN sed -i "s#https\?://[^/]*alpinelinux.org/alpine#${APK_MIRROR}#g" /etc/apk/repositories \
    && apk add --no-cache --virtual .build-deps gcc musl-dev

# Install python deps
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Install rclone (runtime binary)
RUN apk add --no-cache rclone


FROM python:3.11.9-alpine AS runtime

WORKDIR /app

# ffmpeg is required by yt-dlp to merge/transcode videos
ARG APK_MIRROR=https://mirrors.aliyun.com/alpine
RUN sed -i "s#https\?://[^/]*alpinelinux.org/alpine#${APK_MIRROR}#g" /etc/apk/repositories \
    && apk add --no-cache ffmpeg

# Copy installed deps from build stage
COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy rclone to the path expected by the app (matches code default: ./rclone/rclone)
COPY --from=build /usr/bin/rclone /app/rclone/rclone

# Copy app source code
COPY . /app

CMD ["python", "media_downloader.py"]
