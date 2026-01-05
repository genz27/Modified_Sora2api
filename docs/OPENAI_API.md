# OpenAI 标准格式 API 文档

本文档介绍 Sora2API 提供的 OpenAI 标准格式 API 端点。

## 概述

### 生成类 API

| 端点 | 功能 | 输出格式 |
|------|------|----------|
| `POST /v1/videos` | 视频生成 | JSON (非流式) |
| `POST /v1/images/generations` | 图片生成 | JSON (非流式) |
| `POST /v1/characters` | 角色卡创建 | JSON (非流式) |

### 查询类 API

| 端点 | 功能 |
|------|------|
| `GET /v1/stats` | 系统统计信息 |
| `GET /v1/feed` | 公共 Feed |
| `GET /v1/profiles/{username}` | 用户资料 |
| `GET /v1/users/{user_id}/feed` | 用户发布内容 |
| `GET /v1/characters/search` | 搜索角色 |
| `GET /v1/invite-codes` | 获取邀请码 |
| `GET /v1/tokens/{token_id}/profile-feed` | Token 发布内容 |

### 管理类 API (需要管理员认证)

| 端点 | 功能 |
|------|------|
| `POST /api/tokens/batch-add` | 批量添加 Token |
| `POST /api/tokens/batch-test` | 批量测试 Token |
| `POST /api/tokens/batch-activate` | 批量激活 Sora2 |
| `POST /api/tokens/batch-enable` | 批量启用 Token |
| `POST /api/tokens/batch-disable` | 批量禁用 Token |
| `DELETE /api/tokens/batch-delete-disabled` | 批量删除禁用 Token |

所有端点都需要 API Key 认证。生成类 API 支持 `multipart/form-data` 和 `application/json` 两种请求格式。

## 认证

所有请求需要在 Header 中携带 API Key：

```
Authorization: Bearer YOUR_API_KEY
```

默认 API Key: `han1234`

---

## POST /v1/videos

生成视频（默认异步模式）。

### 请求参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `prompt` | string | ✅ | 视频生成提示词 |
| `model` | string | | 模型: `sora-2` 或 `sora-2-pro`，默认 `sora-2` |
| `seconds` | string | | 时长: `"10"` 或 `"15"`，默认 `"15"` |
| `size` | string | | 分辨率: `"720x1280"` (竖屏) 或 `"1280x720"` (横屏) |
| `style_id` | string | | 风格: `festive`, `retro`, `news`, `selfie`, `handheld`, `anime`, `comic`, `golden`, `vintage` |
| `input_reference` | file | | 参考图片文件 (仅 form-data，用于图生视频) |
| `input_image` | string | | Base64 编码的参考图片 (用于图生视频) |
| `remix_target_id` | string | | Remix 视频 ID |
| `async_mode` | boolean | | 异步模式，默认 `true`。设为 `false` 则等待生成完成 |

> **异步模式（默认）**: 立即返回 `id` 和 `status="queued"`，通过 `GET /v1/videos/{id}` 轮询状态
>
> **同步模式**: 设置 `async_mode=false`，等待生成完成后返回结果

### 可用模型和尺寸

| 模型 | 支持尺寸 |
|------|----------|
| `sora-2` | `720x1280`, `1280x720` |
| `sora-2-pro` | `720x1280`, `1280x720`, `1024x1792`, `1792x1024` |

### 请求示例

**multipart/form-data:**

```bash
curl -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer han1234" \
  -F prompt="A cat walking in the garden" \
  -F model="sora-2" \
  -F seconds="15" \
  -F size="720x1280" \
  -F input_reference="@image.jpg;type=image/jpeg"
```

**JSON:**

```bash
curl -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "A cat walking in the garden",
    "model": "sora-2",
    "seconds": "15",
    "size": "720x1280"
  }'
```

### 响应示例

**异步模式（默认）:**

```json
{
  "id": "sora-2-abc123def456",
  "object": "video",
  "model": "sora-2",
  "status": "queued",
  "progress": 0,
  "created_at": 1702388400000,
  "seconds": "15",
  "size": "720x1280"
}
```

**同步模式（async_mode=false）:**

```json
{
  "id": "sora-2-abc123def456",
  "object": "video",
  "model": "sora-2",
  "status": "completed",
  "progress": 100,
  "created_at": 1702388400000,
  "completed_at": 1702388500000,
  "seconds": "15",
  "size": "720x1280",
  "result_url": "https://videos.sora.com/xxx/video.mp4"
}
```

**错误:**

```json
{
  "error": {
    "message": "Video generation failed",
    "type": "server_error",
    "param": null,
    "code": null
  }
}
```

---

## POST /v1/images/generations

生成图片。

### 请求参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `prompt` | string | ✅ | 图片生成提示词 |
| `model` | string | | 模型 ID，默认 `sora-image` |
| `n` | integer | | 生成数量，目前仅支持 `1` |
| `size` | string | | 尺寸: `"1024x1024"`, `"1792x1024"`, `"1024x1792"` |
| `quality` | string | | 质量: `"standard"` 或 `"hd"` |
| `style` | string | | 风格: `"natural"` 或 `"vivid"` |
| `response_format` | string | | 响应格式: `"url"` 或 `"b64_json"` |
| `input_reference` | file | | 参考图片文件 (仅 form-data) |
| `input_image` | string | | Base64 编码的参考图片 |

### 可用模型

| 模型 ID | 尺寸 |
|---------|------|
| `sora-image` | 360x360 (方形) |
| `sora-image-landscape` | 540x360 (横屏) |
| `sora-image-portrait` | 360x540 (竖屏) |

### 请求示例

**multipart/form-data:**

```bash
curl -X POST "http://localhost:8000/v1/images/generations" \
  -H "Authorization: Bearer han1234" \
  -F prompt="A beautiful sunset over mountains" \
  -F model="sora-image-landscape" \
  -F size="1792x1024" \
  -F input_reference="@reference.jpg;type=image/jpeg"
```

**JSON:**

```bash
curl -X POST "http://localhost:8000/v1/images/generations" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "A beautiful sunset over mountains",
    "model": "sora-image-landscape",
    "size": "1792x1024",
    "n": 1,
    "response_format": "url"
  }'
```

### 响应示例

**成功 (OpenAI 标准格式):**

```json
{
  "created": 1702388400,
  "data": [
    {
      "url": "http://localhost:8000/tmp/image_xxx.png",
      "revised_prompt": "A beautiful sunset over mountains"
    }
  ]
}
```

**错误:**

```json
{
  "error": {
    "message": "Image generation failed",
    "type": "server_error",
    "param": null,
    "code": null
  }
}
```

---

## POST /v1/characters

从视频创建角色卡。

### 请求参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `video` | file | ✅* | 角色视频文件 (仅 form-data) |
| `video_base64` | string | ✅* | Base64 编码的视频 |
| `model` | string | | 视频模型，默认 `sora-video-10s` |
| `timestamps` | string | | 角色提取时间戳 (如 `"0,3"`) |
| `username` | string | | 角色用户名 |
| `display_name` | string | | 角色显示名称 |
| `instruction_set` | string | | 角色指令集 |
| `safety_instruction_set` | string | | 安全指令集 |

> ✅* `video` 或 `video_base64` 二选一必填

### 请求示例

**multipart/form-data:**

```bash
curl -X POST "http://localhost:8000/v1/characters" \
  -H "Authorization: Bearer han1234" \
  -F model="sora-video-10s" \
  -F video="@character_video.mp4;type=video/mp4" \
  -F timestamps="0,3" \
  -F username="my_character" \
  -F display_name="My Character"
```

**JSON:**

```bash
curl -X POST "http://localhost:8000/v1/characters" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sora-video-10s",
    "video": "base64_encoded_video_data...",
    "timestamps": "0,3",
    "username": "my_character",
    "display_name": "My Character"
  }'
```

### 响应示例

**成功:**

```json
{
  "id": "char_abc123def456789012345678",
  "object": "character",
  "created": 1702388400,
  "model": "sora-video-10s",
  "data": {
    "cameo_id": "ch_693b0192af888191ac8b3af188acebce",
    "username": "my_character",
    "display_name": "My Character",
    "message": "Character creation completed"
  }
}
```

**错误:**

```json
{
  "error": {
    "message": "video is required for character creation",
    "type": "server_error",
    "param": null,
    "code": null
  }
}
```

---

## 使用角色卡生成视频

创建角色卡后，在 `/v1/videos` 的 `prompt` 中使用 `@username` 引用角色：

```bash
curl -X POST "http://localhost:8000/v1/videos" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "@my_cat running in the park happily",
    "model": "sora-video-10s"
  }'
```

> **提示**: `@username` 是创建角色卡时指定的用户名。

---

## 查询类 API

### GET /v1/stats

获取系统统计信息。

```bash
curl -X GET "http://localhost:8000/v1/stats" \
  -H "Authorization: Bearer han1234"
```

**响应:**

```json
{
  "success": true,
  "stats": {
    "total_tokens": 10,
    "active_tokens": 8,
    "today_images": 50,
    "total_images": 1000,
    "today_videos": 20,
    "total_videos": 500,
    "today_errors": 2,
    "total_errors": 50
  }
}
```

---

### GET /v1/feed

获取公共 Feed。

**参数:**

| 参数 | 类型 | 说明 |
|------|------|------|
| `limit` | int | 返回数量，默认 8 |
| `cut` | string | `nf2_latest` (最新) 或 `nf2_top` (热门) |
| `cursor` | string | 分页游标 |
| `token_id` | int | 指定使用的 Token ID |

```bash
curl -X GET "http://localhost:8000/v1/feed?limit=8&cut=nf2_latest" \
  -H "Authorization: Bearer han1234"
```

**响应:**

```json
{
  "success": true,
  "cut": "nf2_latest",
  "count": 8,
  "cursor": "xxx",
  "items": [
    {
      "id": "post_xxx",
      "text": "视频描述",
      "permalink": "https://sora.chatgpt.com/p/xxx",
      "preview_image_url": "https://...",
      "posted_at": "2024-01-01T00:00:00Z",
      "like_count": 100,
      "view_count": 1000,
      "remix_count": 10,
      "attachment": {
        "kind": "video",
        "url": "https://...",
        "downloadable_url": "https://...",
        "width": 1920,
        "height": 1080,
        "n_frames": 300,
        "duration_seconds": 10
      },
      "author": {
        "user_id": "user-xxx",
        "username": "username",
        "display_name": "Display Name",
        "profile_picture_url": "https://..."
      }
    }
  ]
}
```

---

### GET /v1/profiles/{username}

获取用户资料。

```bash
curl -X GET "http://localhost:8000/v1/profiles/username" \
  -H "Authorization: Bearer han1234"
```

**响应:**

```json
{
  "success": true,
  "profile": {
    "user_id": "user-xxx",
    "username": "username",
    "display_name": "Display Name",
    "profile_picture_url": "https://...",
    "follower_count": 1000
  }
}
```

---

### GET /v1/users/{user_id}/feed

获取用户发布的内容。

**参数:**

| 参数 | 类型 | 说明 |
|------|------|------|
| `limit` | int | 返回数量，默认 8 |
| `cursor` | string | 分页游标 |
| `token_id` | int | 指定使用的 Token ID |

```bash
curl -X GET "http://localhost:8000/v1/users/user-xxx/feed?limit=8" \
  -H "Authorization: Bearer han1234"
```

---

### GET /v1/characters/search

搜索角色。

**参数:**

| 参数 | 类型 | 说明 |
|------|------|------|
| `username` | string | 搜索关键字 |
| `intent` | string | `users` (所有用户) 或 `cameo` (可用于视频生成的角色) |
| `limit` | int | 返回数量，默认 10 |
| `token_id` | int | 指定使用的 Token ID |

```bash
curl -X GET "http://localhost:8000/v1/characters/search?username=test&intent=cameo&limit=10" \
  -H "Authorization: Bearer han1234"
```

**响应:**

```json
{
  "success": true,
  "query": "test",
  "count": 5,
  "results": [
    {
      "user_id": "user-xxx",
      "username": "test_user",
      "display_name": "Test User",
      "profile_picture_url": "https://...",
      "can_cameo": true,
      "token": "<@ch_xxx>"
    }
  ]
}
```

---

### GET /v1/invite-codes

获取随机邀请码。

```bash
curl -X GET "http://localhost:8000/v1/invite-codes" \
  -H "Authorization: Bearer han1234"
```

**响应:**

```json
{
  "success": true,
  "invite_code": "xxx-xxx-xxx",
  "remaining_count": 5,
  "total_count": 10,
  "email": "user@example.com"
}
```

---

### GET /v1/tokens/{token_id}/profile-feed

获取指定 Token 的发布内容。

```bash
curl -X GET "http://localhost:8000/v1/tokens/1/profile-feed?limit=8" \
  -H "Authorization: Bearer han1234"
```

---

## 错误响应格式

所有端点的错误响应遵循 OpenAI 标准格式：

```json
{
  "error": {
    "message": "错误描述信息",
    "type": "error_type",
    "param": null,
    "code": null
  }
}
```

### 常见错误类型

| HTTP 状态码 | 说明 |
|-------------|------|
| 400 | 请求参数错误 |
| 401 | 认证失败 |
| 500 | 服务器内部错误 |

---

## Python 示例

```python
import requests
import base64

API_URL = "http://localhost:8000"
API_KEY = "han1234"
headers = {"Authorization": f"Bearer {API_KEY}"}

# 生成视频
response = requests.post(
    f"{API_URL}/v1/videos",
    headers=headers,
    json={
        "prompt": "A cat playing piano",
        "model": "sora-video-10s",
        "style_id": "anime"
    }
)
result = response.json()
print(f"视频 URL: {result['data'][0]['url']}")

# 生成图片
response = requests.post(
    f"{API_URL}/v1/images/generations",
    headers=headers,
    json={
        "prompt": "A beautiful sunset",
        "model": "sora-image-landscape"
    }
)
result = response.json()
print(f"图片 URL: {result['data'][0]['url']}")

# 创建角色卡
with open("video.mp4", "rb") as f:
    video_b64 = base64.b64encode(f.read()).decode()

response = requests.post(
    f"{API_URL}/v1/characters",
    headers=headers,
    json={
        "video": video_b64,
        "timestamps": "0,3",
        "username": "my_cat",
        "display_name": "我的猫咪"
    }
)
result = response.json()
print(f"角色 ID: {result['data']['cameo_id']}")

# 使用角色卡生成视频
response = requests.post(
    f"{API_URL}/v1/videos",
    headers=headers,
    json={
        "prompt": "running in the garden",
        "character_id": result['data']['cameo_id'],
        "model": "sora-video-10s"
    }
)
result = response.json()
print(f"视频 URL: {result['data'][0]['url']}")
```

---

## JavaScript 示例

```javascript
const API_URL = "http://localhost:8000";
const API_KEY = "han1234";

// 生成视频
async function generateVideo() {
  const response = await fetch(`${API_URL}/v1/videos`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${API_KEY}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      prompt: "A cat playing piano",
      model: "sora-video-10s",
      style_id: "anime"
    })
  });
  
  const result = await response.json();
  console.log("视频 URL:", result.data[0].url);
  return result;
}

// 生成图片
async function generateImage() {
  const response = await fetch(`${API_URL}/v1/images/generations`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${API_KEY}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      prompt: "A beautiful sunset",
      model: "sora-image-landscape"
    })
  });
  
  const result = await response.json();
  console.log("图片 URL:", result.data[0].url);
  return result;
}

// 创建角色卡 (使用 form-data 上传文件)
async function createCharacter(videoFile) {
  const formData = new FormData();
  formData.append("video", videoFile);
  formData.append("timestamps", "0,3");
  formData.append("username", "my_cat");
  formData.append("display_name", "我的猫咪");
  
  const response = await fetch(`${API_URL}/v1/characters`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${API_KEY}`
    },
    body: formData
  });
  
  const result = await response.json();
  console.log("角色 ID:", result.data.cameo_id);
  return result;
}
```


---

## 管理类 API

管理类 API 需要管理员认证，通过登录获取 admin token。

### 认证

```bash
# 登录获取 admin token
curl -X POST "http://localhost:8000/api/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}'

# 使用 admin token
Authorization: Bearer admin-xxxxx
```

---

### POST /api/tokens/batch-add

批量添加多个 Token。

**请求参数:**

| 参数 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `tokens` | array | ✅ | Token 列表 |
| `tokens[].token` | string | ✅ | Access Token |
| `tokens[].st` | string | | Session Token |
| `tokens[].rt` | string | | Refresh Token |
| `tokens[].client_id` | string | | Client ID |
| `tokens[].proxy_url` | string | | 代理 URL |
| `tokens[].remark` | string | | 备注 |
| `tokens[].image_enabled` | boolean | | 启用图片生成 (默认 true) |
| `tokens[].video_enabled` | boolean | | 启用视频生成 (默认 true) |

**请求示例:**

```bash
curl -X POST "http://localhost:8000/api/tokens/batch-add" \
  -H "Authorization: Bearer admin-xxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "tokens": [
      {"token": "eyJhbGciOiJSUzI1NiI...", "remark": "账号1"},
      {"token": "eyJhbGciOiJSUzI1NiI...", "remark": "账号2"}
    ]
  }'
```

**响应示例:**

```json
{
  "success": true,
  "total": 2,
  "added": 2,
  "skipped": 0,
  "failed": 0,
  "details": [
    {"token": "eyJhbGciOiJSUzI1...", "status": "added", "email": "user1@example.com"},
    {"token": "eyJhbGciOiJSUzI1...", "status": "added", "email": "user2@example.com"}
  ]
}
```

---

### POST /api/tokens/batch-test

批量测试 Token 有效性。

**查询参数:**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `only_active` | boolean | true | 仅测试已启用的 Token |
| `only_disabled` | boolean | false | 仅测试已禁用的 Token |

**请求示例:**

```bash
# 测试已启用的 Token (401 自动禁用)
curl -X POST "http://localhost:8000/api/tokens/batch-test?only_active=true" \
  -H "Authorization: Bearer admin-xxxxx"

# 测试已禁用的 Token (有效自动启用)
curl -X POST "http://localhost:8000/api/tokens/batch-test?only_disabled=true" \
  -H "Authorization: Bearer admin-xxxxx"

# 测试所有 Token
curl -X POST "http://localhost:8000/api/tokens/batch-test?only_active=false&only_disabled=false" \
  -H "Authorization: Bearer admin-xxxxx"
```

**响应示例:**

```json
{
  "success": true,
  "message": "测试完成: 8 有效, 2 无效, 1 已自动禁用, 0 已自动启用",
  "total": 10,
  "tested": 10,
  "valid": 8,
  "invalid": 2,
  "auto_disabled": 1,
  "auto_enabled": 0
}
```

---

### POST /api/tokens/batch-activate

批量激活 Sora2 功能。

**请求参数:**

| 参数 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `invite_code` | string | ✅ | 6 位邀请码 |

**请求示例:**

```bash
curl -X POST "http://localhost:8000/api/tokens/batch-activate" \
  -H "Authorization: Bearer admin-xxxxx" \
  -H "Content-Type: application/json" \
  -d '{"invite_code": "ABC123"}'
```

**响应示例:**

```json
{
  "success": true,
  "message": "批量激活完成: 5 激活, 3 已激活, 2 失败",
  "total": 10,
  "activated": 5,
  "already_active": 3,
  "failed": 2
}
```

---

### POST /api/tokens/batch-enable

批量启用所有禁用的 Token。

**请求示例:**

```bash
curl -X POST "http://localhost:8000/api/tokens/batch-enable" \
  -H "Authorization: Bearer admin-xxxxx"
```

**响应示例:**

```json
{
  "success": true,
  "message": "已启用 5 个账号",
  "enabled_count": 5
}
```

---

### POST /api/tokens/batch-disable

批量禁用所有启用的 Token。

**请求示例:**

```bash
curl -X POST "http://localhost:8000/api/tokens/batch-disable" \
  -H "Authorization: Bearer admin-xxxxx"
```

**响应示例:**

```json
{
  "success": true,
  "message": "已禁用 10 个账号",
  "disabled_count": 10
}
```

---

### DELETE /api/tokens/batch-delete-disabled

批量删除所有禁用的 Token。

**请求示例:**

```bash
curl -X DELETE "http://localhost:8000/api/tokens/batch-delete-disabled" \
  -H "Authorization: Bearer admin-xxxxx"
```

**响应示例:**

```json
{
  "success": true,
  "message": "已删除 3 个禁用账号",
  "deleted_count": 3
}
```
