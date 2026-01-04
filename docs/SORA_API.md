# OpenAI Sora 视频生成 API

支持 OpenAI Sora 官方 API 格式的视频生成接口。

## 自适应轮询机制

Sora2API 实现了智能的自适应轮询机制，根据视频生成进度动态调整轮询间隔：

| 进度范围 | 轮询间隔 | 说明 |
|----------|----------|------|
| 0% - 30% | 5 秒 | 初始阶段，进度较慢 |
| 30% - 70% | 3 秒 | 中间阶段，进度加快 |
| 70% - 100% | 2 秒 | 最终阶段，即将完成 |

### 停滞检测

系统会自动检测生成停滞情况：
- 当连续 3 次轮询进度无变化时，自动增加轮询间隔
- 每次停滞检测后，间隔增加 2 秒
- 最大轮询间隔限制为 10 秒
- 当进度恢复变化时，自动重置为正常间隔

这种机制可以：
- 减少不必要的 API 请求
- 在进度快速变化时及时获取更新
- 在停滞时避免频繁无效请求

## 可用模型

| 模型 ID | 时长 | 方向 |
|---------|------|------|
| sora-video-10s | 10秒 | 横屏 |
| sora-video-15s | 15秒 | 横屏 |
| sora-video-25s | 25秒 | 横屏 |
| sora-video-landscape-10s | 10秒 | 横屏 |
| sora-video-landscape-15s | 15秒 | 横屏 |
| sora-video-landscape-25s | 25秒 | 横屏 |
| sora-video-portrait-10s | 10秒 | 竖屏 |
| sora-video-portrait-15s | 15秒 | 竖屏 |
| sora-video-portrait-25s | 25秒 | 竖屏 |

## API 端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/v1/videos` | POST | 创建视频生成任务 |
| `/v1/videos/{video_id}` | GET | 查询视频任务状态 |
| `/v1/videos/{video_id}/content` | GET | 下载视频内容 |
| `/v1/chat/completions` | POST | 聊天补全接口（统一端点） |
| `/v1/models` | GET | 列出可用模型 |

---

## 创建视频

### POST /v1/videos

创建视频生成任务，支持文生视频和图生视频。

#### 请求头

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| Authorization | string | 是 | Bearer token (Bearer sk-xxxx) |
| Content-Type | string | 是 | multipart/form-data 或 application/json |

#### 请求参数

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| prompt | string | 是 | 视频描述提示词 |
| model | string | 否 | 模型名称，默认 `sora-video-landscape-10s`，支持 `sora-2` |
| seconds | string | 否 | 视频时长：`10`、`15` 或 `25`，默认 `10` |
| size | string | 否 | 分辨率，如 `1920x1080`、`1080x1920` |
| orientation | string | 否 | 方向：`landscape` 或 `portrait` |
| style_id | string | 否 | 视频风格：festive, retro, news, selfie, handheld, anime, comic, golden, vintage |
| input_reference | file | 否 | 参考图片文件（图生视频，仅 multipart/form-data） |
| input_image | string | 否 | Base64 编码的参考图片 |
| remix_target_id | string | 否 | 混剪源视频ID（如 `s_xxx`） |
| async_mode | boolean | 否 | 异步模式，默认 `false`。设为 `true` 时立即返回任务ID |
| metadata | string | 否 | 扩展参数（JSON字符串） |

#### 同步模式（默认）

等待视频生成完成后返回结果。

**请求示例：**

```bash
# 文生视频
curl -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer sk-xxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一只可爱的猫咪在花园里玩耍，阳光明媚",
    "model": "sora-2",
    "seconds": "10",
    "size": "1920x1080"
  }'

# 图生视频（multipart/form-data）
curl -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer sk-xxxx" \
  -F "prompt=让图片中的猫咪慢慢睁开眼睛" \
  -F "model=sora-2" \
  -F "seconds=10" \
  -F "input_reference=@/path/to/cat.jpg"
```

**响应示例 (201 Created)：**

```json
{
  "id": "video_a1b2c3d4e5f6g7h8i9j0k1l2",
  "object": "video",
  "model": "sora-2",
  "created_at": 1703145600,
  "status": "succeeded",
  "progress": 100,
  "expires_at": 1703232000,
  "size": "1920x1080",
  "seconds": "10",
  "quality": "standard",
  "url": "https://videos.sora.com/xxx/video.mp4",
  "permalink": "https://sora.chatgpt.com/p/s_xxx"
}
```

#### 异步模式

设置 `async_mode=true` 时，立即返回任务ID，通过轮询查询状态。

**请求示例：**

```bash
curl -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer sk-xxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一只猫咪在草地上奔跑",
    "model": "sora-2",
    "seconds": "10",
    "async_mode": true
  }'
```

**响应示例 (201 Created)：**

```json
{
  "id": "video_a1b2c3d4e5f6g7h8i9j0k1l2",
  "object": "video",
  "model": "sora-2",
  "created_at": 1703145600,
  "status": "processing",
  "progress": 0,
  "expires_at": 1703232000,
  "size": "1920x1080",
  "seconds": "10",
  "quality": "standard"
}
```

---

## 查询视频状态

### GET /v1/videos/{video_id}

查询视频生成任务的状态和结果。

#### 路径参数

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| video_id | string | 是 | 视频任务ID |

#### 请求示例

```bash
curl "http://localhost:8000/v1/videos/video_a1b2c3d4e5f6g7h8i9j0k1l2" \
  -H "Authorization: Bearer sk-xxxx"
```

#### 响应示例 - 处理中

```json
{
  "id": "video_a1b2c3d4e5f6g7h8i9j0k1l2",
  "object": "video",
  "model": "sora-video-landscape-10s",
  "created_at": 1703145600,
  "status": "processing",
  "progress": 45,
  "expires_at": 1703232000,
  "size": "1920x1080",
  "seconds": "10",
  "quality": "standard",
  "remixed_from_video_id": null,
  "error": null
}
```

#### 响应示例 - 成功

```json
{
  "id": "video_a1b2c3d4e5f6g7h8i9j0k1l2",
  "object": "video",
  "model": "sora-video-landscape-10s",
  "created_at": 1703145600,
  "status": "succeeded",
  "progress": 100,
  "expires_at": 1703232000,
  "size": "1920x1080",
  "seconds": "10",
  "quality": "standard",
  "url": "https://videos.sora.com/xxx/video.mp4",
  "remixed_from_video_id": null,
  "error": null
}
```

#### 响应示例 - 失败

```json
{
  "id": "video_a1b2c3d4e5f6g7h8i9j0k1l2",
  "object": "video",
  "model": "sora-video-landscape-10s",
  "created_at": 1703145600,
  "status": "failed",
  "progress": 0,
  "expires_at": 1703232000,
  "size": "1920x1080",
  "seconds": "10",
  "quality": "standard",
  "remixed_from_video_id": null,
  "error": {
    "message": "Content policy violation",
    "type": "server_error"
  }
}
```

#### 响应字段说明

| 字段 | 类型 | 描述 |
|------|------|------|
| id | string | 视频任务ID |
| object | string | 对象类型，固定为 `video` |
| model | string | 使用的模型名称 |
| created_at | integer | 创建时间戳（秒） |
| status | string | 状态：`processing`, `succeeded`, `failed` |
| progress | integer | 进度百分比 (0-100) |
| expires_at | integer | 资源过期时间戳 |
| size | string | 视频分辨率 |
| seconds | string | 视频时长 |
| quality | string | 视频质量 |
| url | string | 视频下载链接（成功时） |
| remixed_from_video_id | string | 混剪源视频ID |
| error | object | 错误信息（失败时） |

---

## 下载视频内容

### GET /v1/videos/{video_id}/content

下载已完成的视频文件。

#### 路径参数

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| video_id | string | 是 | 视频任务ID |

#### 查询参数

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| variant | string | 否 | 资源类型，默认 `mp4` |

#### 请求示例

```bash
curl "http://localhost:8000/v1/videos/video_a1b2c3d4e5f6g7h8i9j0k1l2/content" \
  -H "Authorization: Bearer sk-xxxx" \
  -o "output.mp4"
```

#### 响应

返回 302 重定向到视频 URL。

| HTTP 状态码 | 描述 |
|-------------|------|
| 302 | 重定向到视频下载链接 |
| 400 | 视频未准备好或生成失败 |
| 404 | 视频任务不存在 |

---

## 聊天补全接口

### POST /v1/chat/completions

统一的聊天补全接口，支持图片和视频生成。

#### 请求示例

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer sk-xxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sora-video-landscape-10s",
    "messages": [
      {"role": "user", "content": "一只猫咪在草地上奔跑"}
    ],
    "stream": true
  }'
```

#### 多模态请求（图生视频）

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer sk-xxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sora-video-landscape-10s",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "让图片中的猫咪动起来"},
          {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        ]
      }
    ],
    "stream": true
  }'
```

---

## 错误响应

所有错误响应格式统一：

```json
{
  "error": {
    "message": "错误描述信息",
    "type": "错误类型"
  }
}
```

### 错误类型

| HTTP 状态码 | type | 描述 |
|-------------|------|------|
| 400 | invalid_request_error | 请求参数错误 |
| 401 | invalid_request_error | 未授权 |
| 404 | invalid_request_error | 任务不存在 |
| 500 | server_error | 服务器内部错误 |

---

## 完整使用流程（异步模式）

```bash
# 1. 创建视频任务（异步模式）
VIDEO_ID=$(curl -s -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer sk-xxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一只猫咪在草地上奔跑",
    "seconds": "10",
    "async_mode": true
  }' | jq -r '.id')

echo "Task ID: $VIDEO_ID"

# 2. 轮询查询状态
while true; do
  RESPONSE=$(curl -s "http://localhost:8000/v1/videos/$VIDEO_ID" \
    -H "Authorization: Bearer sk-xxxx")
  
  STATUS=$(echo $RESPONSE | jq -r '.status')
  PROGRESS=$(echo $RESPONSE | jq -r '.progress')
  
  echo "Status: $STATUS, Progress: $PROGRESS%"
  
  if [ "$STATUS" = "succeeded" ]; then
    URL=$(echo $RESPONSE | jq -r '.url')
    echo "Video URL: $URL"
    break
  elif [ "$STATUS" = "failed" ]; then
    ERROR=$(echo $RESPONSE | jq -r '.error.message')
    echo "Generation failed: $ERROR"
    exit 1
  fi
  
  sleep 5
done

# 3. 下载视频
curl "http://localhost:8000/v1/videos/$VIDEO_ID/content" \
  -H "Authorization: Bearer sk-xxxx" \
  -L -o "video.mp4"

echo "Video saved to video.mp4"
```

## 完整使用流程（同步模式）

```bash
# 直接创建并等待完成
curl -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer sk-xxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一只猫咪在草地上奔跑",
    "seconds": "10"
  }' | jq '.'
```
