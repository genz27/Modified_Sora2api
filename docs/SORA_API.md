# OpenAI Sora 视频生成 API

支持 OpenAI Sora 官方 API 格式的视频生成接口。

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

---

## 创建视频

### POST /v1/videos

创建视频生成任务，支持文生视频和图生视频。

#### 请求头

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| Authorization | string | 是 | Bearer token (Bearer sk-xxxx) |
| Content-Type | string | 是 | multipart/form-data |

#### 请求参数 (multipart/form-data)

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| prompt | string | 是 | 视频描述提示词 |
| model | string | 否 | 模型名称，默认 `sora-2` |
| seconds | string | 否 | 视频时长（秒），默认 `4` |
| size | string | 否 | 分辨率，默认 `720x1280` |
| input_reference | file | 否 | 参考图片文件（图生视频） |
| metadata | string | 否 | 扩展参数（JSON字符串） |

#### 请求示例

**文生视频：**

```bash
curl -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer sk-xxxx" \
  -F "prompt=一只可爱的猫咪在花园里玩耍，阳光明媚" \
  -F "model=sora-2" \
  -F "seconds=5" \
  -F "size=1920x1080"
```

**图生视频：**

```bash
curl -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer sk-xxxx" \
  -F "prompt=让图片中的猫咪慢慢睁开眼睛" \
  -F "model=sora-2" \
  -F "seconds=5" \
  -F "size=1920x1080" \
  -F "input_reference=@/path/to/cat.jpg"
```

**带扩展参数：**

```bash
curl -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer sk-xxxx" \
  -F "prompt=一只猫咪在奔跑" \
  -F "model=sora-2" \
  -F "seconds=10" \
  -F "size=1920x1080" \
  -F 'metadata={"style_id":"anime"}'
```

#### 响应示例 (201 Created)

```json
{
  "id": "video_a1b2c3d4e5f6",
  "object": "video",
  "model": "sora-2",
  "created_at": 1703145600,
  "status": "processing",
  "progress": 0
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
curl "http://localhost:8000/v1/videos/video_a1b2c3d4e5f6" \
  -H "Authorization: Bearer sk-xxxx"
```

#### 响应示例 - 处理中

```json
{
  "id": "video_a1b2c3d4e5f6",
  "object": "video",
  "model": "sora-2",
  "created_at": 1703145600,
  "status": "processing",
  "progress": 45,
  "expires_at": 1703232000,
  "size": "1920x1080",
  "seconds": "5",
  "quality": "standard",
  "remixed_from_video_id": null,
  "error": null
}
```

#### 响应示例 - 成功

```json
{
  "id": "video_a1b2c3d4e5f6",
  "object": "video",
  "model": "sora-2",
  "created_at": 1703145600,
  "status": "succeeded",
  "progress": 100,
  "expires_at": 1703232000,
  "size": "1920x1080",
  "seconds": "5",
  "quality": "standard",
  "url": "https://videos.sora.com/xxx/video.mp4",
  "remixed_from_video_id": null,
  "error": null
}
```

#### 响应示例 - 失败

```json
{
  "id": "video_a1b2c3d4e5f6",
  "object": "video",
  "model": "sora-2",
  "created_at": 1703145600,
  "status": "failed",
  "progress": 0,
  "expires_at": 1703232000,
  "size": "1920x1080",
  "seconds": "5",
  "quality": "standard",
  "remixed_from_video_id": null,
  "error": {
    "message": "Content policy violation",
    "type": "invalid_request_error"
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
curl "http://localhost:8000/v1/videos/video_a1b2c3d4e5f6/content" \
  -H "Authorization: Bearer sk-xxxx" \
  -o "output.mp4"
```

#### 响应

直接返回视频文件流。

| 响应头 | 描述 |
|--------|------|
| Content-Type | `video/mp4` |
| Content-Disposition | `attachment; filename="video_xxx.mp4"` |

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

## 完整使用流程

```bash
# 1. 创建视频任务
VIDEO_ID=$(curl -s -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer sk-xxxx" \
  -F "prompt=一只猫咪在草地上奔跑" \
  -F "seconds=5" | jq -r '.id')

echo "Task ID: $VIDEO_ID"

# 2. 轮询查询状态
while true; do
  STATUS=$(curl -s "http://localhost:8000/v1/videos/$VIDEO_ID" \
    -H "Authorization: Bearer sk-xxxx" | jq -r '.status')
  
  echo "Status: $STATUS"
  
  if [ "$STATUS" = "succeeded" ]; then
    break
  elif [ "$STATUS" = "failed" ]; then
    echo "Generation failed!"
    exit 1
  fi
  
  sleep 5
done

# 3. 下载视频
curl "http://localhost:8000/v1/videos/$VIDEO_ID/content" \
  -H "Authorization: Bearer sk-xxxx" \
  -o "video.mp4"

echo "Video saved to video.mp4"
```
