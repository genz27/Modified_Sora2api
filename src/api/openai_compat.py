"""OpenAI-compatible API endpoints for Sora generation

Provides standard OpenAI API format for:
- /v1/chat/completions - Chat completion (unified endpoint for image and video generation)
- /v1/videos - Video generation (supports multipart/form-data and JSON)
- /v1/images/generations - Image generation (supports multipart/form-data and JSON)
- /v1/characters - Character creation (supports multipart/form-data and JSON)
- /v1/models - List available models
"""
from fastapi import APIRouter, HTTPException, Depends, Form, File, UploadFile, Request
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Optional, List, Union
from pydantic import BaseModel
import base64
import json
import time
import uuid
import re
from ..core.auth import verify_api_key_header
from ..services.generation_handler import GenerationHandler, MODEL_CONFIG
from ..core.models import CharacterOptions, ChatCompletionRequest

router = APIRouter()

# Dependency injection
generation_handler: GenerationHandler = None


def set_generation_handler(handler: GenerationHandler):
    """Set generation handler instance"""
    global generation_handler
    generation_handler = handler


def _extract_remix_id(text: str) -> str:
    """Extract remix ID from text

    Supports two formats:
    1. Full URL: https://sora.chatgpt.com/p/s_68e3a06dcd888191b150971da152c1f5
    2. Short ID: s_68e3a06dcd888191b150971da152c1f5

    Args:
        text: Text to search for remix ID

    Returns:
        Remix ID (s_[a-f0-9]{32}) or empty string if not found
    """
    if not text:
        return ""

    # Match Sora share link format: s_[a-f0-9]{32}
    match = re.search(r's_[a-f0-9]{32}', text)
    if match:
        return match.group(0)

    return ""


# ============================================================
# /v1/models - List Available Models
# ============================================================

@router.get("/v1/models")
async def list_models(api_key: str = Depends(verify_api_key_header)):
    """List available models"""
    models = []
    
    for model_id, config in MODEL_CONFIG.items():
        description = f"{config['type'].capitalize()} generation"
        if config['type'] == 'image':
            description += f" - {config['width']}x{config['height']}"
        else:
            description += f" - {config['orientation']}"
        
        models.append({
            "id": model_id,
            "object": "model",
            "owned_by": "sora2api",
            "description": description
        })
    
    return {
        "object": "list",
        "data": models
    }


# ============================================================
# /v1/chat/completions - Chat Completion (Unified Endpoint)
# ============================================================

@router.post("/v1/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    api_key: str = Depends(verify_api_key_header)
):
    """Create chat completion (unified endpoint for image and video generation)
    
    Supports both streaming and non-streaming responses.
    
    **Request format:**
    ```json
    {
        "model": "sora-video-10s",
        "messages": [
            {"role": "user", "content": "A cat walking in the park"}
        ],
        "stream": true,
        "image": "base64_encoded_image",  // optional, for image-to-video
        "video": "base64_encoded_video",  // optional, for character creation
        "remix_target_id": "s_xxx",       // optional, for remix
        "style_id": "anime"               // optional, video style
    }
    ```
    
    **Multimodal content format:**
    ```json
    {
        "model": "sora-video-10s",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "A cat walking"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}
                ]
            }
        ],
        "stream": true
    }
    ```
    
    **Streaming response format (SSE):**
    ```
    data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1234567890,"model":"sora","choices":[{"index":0,"delta":{"role":"assistant","content":null,"reasoning_content":{"stage":"generation","status":"processing","progress":50,"message":"..."}},"finish_reason":null}]}
    
    data: [DONE]
    ```
    
    **Non-streaming response:**
    Returns availability check message only. Use stream=true for actual generation.
    """
    try:
        # Extract prompt from messages
        if not request.messages:
            raise HTTPException(status_code=400, detail="Messages cannot be empty")

        last_message = request.messages[-1]
        content = last_message.content

        # Handle both string and array format (OpenAI multimodal)
        prompt = ""
        image_data = request.image  # Default to request.image if provided
        video_data = request.video  # Video parameter
        remix_target_id = request.remix_target_id  # Remix target ID

        if isinstance(content, str):
            # Simple string format
            prompt = content
            # Extract remix_target_id from prompt if not already provided
            if not remix_target_id:
                remix_target_id = _extract_remix_id(prompt)
        elif isinstance(content, list):
            # Array format (OpenAI multimodal)
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        prompt = item.get("text", "")
                        # Extract remix_target_id from prompt if not already provided
                        if not remix_target_id:
                            remix_target_id = _extract_remix_id(prompt)
                    elif item.get("type") == "image_url":
                        # Extract base64 image from data URI
                        image_url = item.get("image_url", {})
                        url = image_url.get("url", "")
                        if url.startswith("data:image"):
                            # Extract base64 data from data URI
                            if "base64," in url:
                                image_data = url.split("base64,", 1)[1]
                            else:
                                image_data = url
                    elif item.get("type") == "video_url":
                        # Extract video from video_url
                        video_url = item.get("video_url", {})
                        url = video_url.get("url", "")
                        if url.startswith("data:video") or url.startswith("data:application"):
                            # Extract base64 data from data URI
                            if "base64," in url:
                                video_data = url.split("base64,", 1)[1]
                            else:
                                video_data = url
                        else:
                            # It's a URL, pass it as-is (will be downloaded in generation_handler)
                            video_data = url
        else:
            raise HTTPException(status_code=400, detail="Invalid content format")

        # Validate model
        if request.model not in MODEL_CONFIG:
            raise HTTPException(status_code=400, detail=f"Invalid model: {request.model}")

        # Check if this is a video model
        model_config = MODEL_CONFIG[request.model]
        is_video_model = model_config["type"] == "video"

        # For video models with video parameter, we need streaming
        if is_video_model and (video_data or remix_target_id):
            if not request.stream:
                # Non-streaming mode: only check availability
                result = None
                async for chunk in generation_handler.handle_generation(
                    model=request.model,
                    prompt=prompt,
                    image=image_data,
                    video=video_data,
                    remix_target_id=remix_target_id,
                    stream=False,
                    style_id=request.style_id
                ):
                    result = chunk

                if result:
                    return JSONResponse(content=json.loads(result))
                else:
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": {
                                "message": "Availability check failed",
                                "type": "server_error",
                                "param": None,
                                "code": None
                            }
                        }
                    )

        # Handle streaming
        if request.stream:
            async def generate():
                has_error = False
                error_message = None
                try:
                    async for chunk in generation_handler.handle_generation(
                        model=request.model,
                        prompt=prompt,
                        image=image_data,
                        video=video_data,
                        remix_target_id=remix_target_id,
                        stream=True,
                        style_id=request.style_id
                    ):
                        yield chunk
                except GeneratorExit:
                    # Client disconnected, clean exit
                    pass
                except Exception as e:
                    has_error = True
                    error_message = str(e)
                    import traceback
                    traceback.print_exc()
                
                # If error occurred, send OpenAI-compatible error in stream format
                if has_error:
                    error_chunk = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": request.model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "content": json.dumps({
                                    "type": "error",
                                    "error": error_message
                                })
                            },
                            "finish_reason": "stop"
                        }]
                    }
                    yield f'data: {json.dumps(error_chunk)}\n\n'
                    yield 'data: [DONE]\n\n'

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no"
                }
            )
        else:
            # Non-streaming response (availability check only)
            result = None
            async for chunk in generation_handler.handle_generation(
                model=request.model,
                prompt=prompt,
                image=image_data,
                video=video_data,
                remix_target_id=remix_target_id,
                stream=False,
                style_id=request.style_id
            ):
                result = chunk

            if result:
                return JSONResponse(content=json.loads(result))
            else:
                # Return OpenAI-compatible error format
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": {
                            "message": "Availability check failed",
                            "type": "server_error",
                            "param": None,
                            "code": None
                        }
                    }
                )

    except HTTPException:
        raise
    except Exception as e:
        # Return OpenAI-compatible error format
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": str(e),
                    "type": "server_error",
                    "param": None,
                    "code": None
                }
            }
        )


def _extract_url_from_chunks(chunks_data: list) -> Optional[str]:
    """Extract URL from streaming chunks"""
    for chunk in chunks_data:
        if chunk.startswith("data: ") and chunk != "data: [DONE]\n\n":
            try:
                data = json.loads(chunk[6:])
                choices = data.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    # Look for URL in content
                    if content:
                        url_match = re.search(r'https?://[^\s\]"\']+', content)
                        if url_match:
                            return url_match.group(0)
            except Exception:
                pass
    return None


def _extract_video_info_from_chunks(chunks_data: list) -> dict:
    """Extract video info (url/permalink) from streaming chunks.

    Prefers parsing the structured JSON string produced by GenerationHandler,
    and falls back to regex extraction when needed.
    """
    for chunk in chunks_data:
        if chunk.startswith("data: ") and chunk != "data: [DONE]\n\n":
            try:
                data = json.loads(chunk[6:])
                choices = data.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if not content:
                    continue

                # Structured result content is a JSON string, e.g. {"type":"video","url":...}
                try:
                    structured = json.loads(content)
                except Exception:
                    structured = None

                if isinstance(structured, dict) and structured.get("type") == "video":
                    url = structured.get("url")
                    permalink = structured.get("permalink")
                    data_items = structured.get("data") or []
                    if not url and data_items and isinstance(data_items[0], dict):
                        url = data_items[0].get("url")
                    if not permalink and data_items and isinstance(data_items[0], dict):
                        permalink = data_items[0].get("permalink")
                    return {"url": url, "permalink": permalink}

                # Fallback: extract by regex
                url_match = re.search(r'https?://[^\s\]"\']+', content)
                if url_match:
                    return {"url": url_match.group(0), "permalink": None}
            except Exception:
                pass
    return {}


def _extract_character_info(chunks_data: list) -> dict:
    """Extract character info from streaming chunks"""
    result = {}
    for chunk in chunks_data:
        if chunk.startswith("data: ") and chunk != "data: [DONE]\n\n":
            try:
                data = json.loads(chunk[6:])
                choices = data.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    # Check metadata for character info
                    metadata = delta.get("metadata", {})
                    if metadata.get("character"):
                        result.update(metadata)
                    # Check content for character info
                    content = delta.get("content", "")
                    if content:
                        # Extract cameo_id pattern
                        cameo_match = re.search(r'ch_[a-f0-9]{32}', content)
                        if cameo_match:
                            result["cameo_id"] = cameo_match.group(0)
                        # Store message
                        if "character" in content.lower() or "cameo" in content.lower():
                            result["message"] = content
            except Exception:
                pass
    return result


# ============================================================
# /v1/videos - Video Generation (OpenAI Sora Compatible)
# Compatible with new-api-main sora2 relay format
# ============================================================

# Background task storage for async video generation
import asyncio
_video_tasks: dict = {}  # video_id -> task_info dict


async def _process_video_generation_v2(video_id: str):
    """Background task to process video generation (updates in-memory task)
    
    Compatible with new-api-main sora relay format.
    """
    import re
    from ..core.database import Database
    
    task_info = _video_tasks.get(video_id)
    if not task_info or not isinstance(task_info, dict):
        print(f"[VideoTask] {video_id}: task_info not found in _video_tasks")
        return
    
    db = Database()
    
    try:
        task_info["status"] = "in_progress"
        print(f"[VideoTask] {video_id}: Starting generation...")
        
        # Generate video
        result_url = None
        last_chunk = None
        chunk_count = 0
        
        async for chunk in generation_handler.handle_generation(
            model=task_info["internal_model"],
            prompt=task_info["prompt"],
            image=task_info.get("image"),
            remix_target_id=task_info.get("remix_target_id"),
            stream=True,
            style_id=task_info.get("style_id")
        ):
            chunk_count += 1
            if isinstance(chunk, str):
                last_chunk = chunk
                # Extract progress percentage if present
                match = re.search(r'(\d+)%', chunk)
                if match:
                    progress = int(match.group(1))
                    task_info["progress"] = progress
                
                # Check for result URL in chunk - try multiple patterns
                # Pattern 1: URL in video tag (e.g., <video src='url'>)
                video_src_match = re.search(r"<video[^>]+src=['\"]([^'\"]+)['\"]", chunk)
                if video_src_match:
                    result_url = video_src_match.group(1)
                    print(f"[VideoTask] {video_id}: Found URL in video tag: {result_url[:100]}...")
                
                # Pattern 2: Direct .mp4 URL (more permissive)
                if not result_url:
                    url_match = re.search(r'(https?://[^\s\]"<>]+\.mp4[^\s\]"<>]*)', chunk)
                    if url_match:
                        result_url = url_match.group(1).rstrip("'").rstrip('"')
                        print(f"[VideoTask] {video_id}: Found MP4 URL: {result_url[:100]}...")
                
                # Pattern 3: URL in JSON content field
                if not result_url:
                    try:
                        # Try to parse as SSE data
                        if chunk.startswith("data: ") and chunk != "data: [DONE]\n\n":
                            data = json.loads(chunk[6:])
                            choices = data.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    # Check for video tag in content
                                    video_match = re.search(r"<video[^>]+src=['\"]([^'\"]+)['\"]", content)
                                    if video_match:
                                        result_url = video_match.group(1)
                                        print(f"[VideoTask] {video_id}: Found URL in content video tag: {result_url[:100]}...")
                                    else:
                                        # Try to parse content as JSON
                                        try:
                                            content_data = json.loads(content)
                                            if isinstance(content_data, dict):
                                                url = content_data.get("url")
                                                if url and ("mp4" in url or "video" in url or "tmp/" in url):
                                                    result_url = url
                                                    print(f"[VideoTask] {video_id}: Found URL in content JSON: {result_url[:100]}...")
                                        except:
                                            pass
                    except:
                        pass
                
                # Pattern 4: Any URL with video-like extensions or paths
                if not result_url:
                    any_url_match = re.search(r"(https?://[^\s'\"<>\]]+(?:\.mp4|/tmp/|/video)[^\s'\"<>\]]*)", chunk)
                    if any_url_match:
                        result_url = any_url_match.group(1)
                        print(f"[VideoTask] {video_id}: Found URL with pattern 4: {result_url[:100]}...")
        
        print(f"[VideoTask] {video_id}: Generation loop finished. chunk_count={chunk_count}, result_url={result_url is not None}")
        if last_chunk:
            print(f"[VideoTask] {video_id}: Last chunk (truncated): {last_chunk[:200]}...")
        
        # Try to get result from database if not found in stream
        if not result_url:
            print(f"[VideoTask] {video_id}: No URL found in stream, checking database...")
            all_tasks = await db.get_recent_tasks(limit=10)
            for db_task in all_tasks:
                if db_task.prompt == task_info["prompt"] and db_task.status == "completed":
                    if db_task.result_urls:
                        try:
                            urls = json.loads(db_task.result_urls)
                            if urls:
                                result_url = urls[0] if isinstance(urls, list) else urls
                        except:
                            result_url = db_task.result_urls
                        print(f"[VideoTask] {video_id}: Found URL in database: {result_url[:100] if result_url else 'None'}...")
                        break
        
        # Mark as completed (use new-api-main compatible status)
        print(f"[VideoTask] {video_id}: Marking as completed. result_url={result_url is not None}")
        task_info["status"] = "completed"
        task_info["progress"] = 100
        task_info["completed_at"] = int(time.time())  # Unix timestamp in seconds
        task_info["result_url"] = result_url
        
        # Update database
        if result_url:
            await db.update_task(video_id, "completed", 100.0, result_urls=result_url)
        else:
            await db.update_task(video_id, "completed", 100.0)
        
        print(f"[VideoTask] {video_id}: Task completed successfully. Status in memory: {task_info['status']}")
    
    except Exception as e:
        print(f"[VideoTask] {video_id}: Exception occurred: {str(e)}")
        import traceback
        traceback.print_exc()
        task_info["status"] = "failed"
        task_info["error"] = {
            "message": str(e),
            "code": "generation_failed"
        }
        try:
            await db.update_task(video_id, "failed", 0.0, error_message=str(e))
        except Exception:
            pass

@router.post("/v1/videos", status_code=200)
async def create_video(
    request: Request,
    prompt: str = Form(None, description="Video generation prompt"),
    model: str = Form("sora-2", description="Model ID: sora-2 or sora-2-pro"),
    seconds: Optional[str] = Form(None, description="Duration: '10' or '15'"),
    size: Optional[str] = Form(None, description="Output resolution: '720x1280' (portrait) or '1280x720' (landscape)"),
    orientation: Optional[str] = Form(None, description="Orientation: 'landscape' or 'portrait'"),
    style_id: Optional[str] = Form(None, description="Video style"),
    input_reference: Optional[UploadFile] = File(None, description="Reference image file"),
    input_image: Optional[str] = Form(None, description="Base64 encoded reference image"),
    remix_target_id: Optional[str] = Form(None, description="Remix target video ID"),
    metadata: Optional[str] = Form(None, description="Extended parameters (JSON string)"),
    async_mode: Optional[bool] = Form(True, description="Async mode: return immediately with task ID (default: true)"),
    api_key: str = Depends(verify_api_key_header)
):
    """Create video generation (Sora Compatible - new-api-main format)
    
    Supports both multipart/form-data and JSON body.
    Compatible with new-api-main sora2 relay format.
    
    **Async Mode (default):**
    - Returns immediately with id and status="in_progress"
    - Poll GET /v1/videos/{id} to check status
    - Download via GET /v1/videos/{id}/content when completed
    
    **Sync Mode (async_mode=false):**
    - Waits for generation to complete
    - Returns final result with status="completed"
    
    **Response format (new-api-main compatible):**
    ```json
    {
        "id": "sora-2-abc123def456",
        "object": "video",
        "model": "sora-2",
        "status": "in_progress",
        "progress": 0,
        "created_at": 1702388400,
        "seconds": "10",
        "size": "1280x720"
    }
    ```
    """
    try:
        # Check if JSON body
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            prompt = body.get("prompt", prompt)
            model = body.get("model", model)
            seconds = body.get("seconds", seconds)
            size = body.get("size", size)
            orientation = body.get("orientation", orientation)
            style_id = body.get("style_id", style_id)
            input_image = body.get("input_image", input_image)
            input_image = body.get("input_reference", input_image)  # Also support input_reference
            remix_target_id = body.get("remix_target_id", remix_target_id)
            metadata = body.get("metadata", metadata)
            async_mode = body.get("async_mode", async_mode)
        
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        
        # Normalize model name
        if model in ["sora", "sora-2", "sora2"]:
            model = "sora-2"
        elif model in ["sora-2-pro", "sora2-pro", "sora2pro"]:
            model = "sora-2-pro"
        
        # Validate model
        valid_models = ["sora-2", "sora-2-pro"]
        if model not in valid_models:
            raise HTTPException(status_code=400, detail=f"Unsupported model: {model}. Valid: {', '.join(valid_models)}")
        
        # Valid sizes per model
        valid_sizes = {
            "sora-2": ["720x1280", "1280x720"],
            "sora-2-pro": ["720x1280", "1280x720", "1024x1792", "1792x1024"],
        }
        
        # Default size based on orientation
        if not size:
            if orientation == "portrait":
                size = "720x1280"
            else:
                size = "1280x720"
        
        # Validate size
        if size not in valid_sizes[model]:
            raise HTTPException(status_code=400, detail=f"Invalid size for {model}. Valid sizes: {valid_sizes[model]}")
        
        # Parse seconds
        duration = 15  # default
        if seconds:
            try:
                duration = int(seconds)
            except:
                duration = 15
        
        # Validate seconds
        valid_seconds = [10, 15]
        if duration not in valid_seconds:
            raise HTTPException(status_code=400, detail=f"Invalid seconds: {duration}. Valid: 10, 15")
        
        # Determine orientation from size
        try:
            width, height = map(int, size.split('x'))
            orient = "landscape" if width > height else "portrait"
        except:
            orient = "portrait"
        
        # Map to internal model (sora-video-{orientation}-{duration}s format)
        final_model = f"sora-video-{orient}-{duration}s"
        
        # Process reference image
        image_data = None
        if input_reference:
            content = await input_reference.read()
            image_data = base64.b64encode(content).decode('utf-8')
        elif input_image:
            image_data = input_image
            if "base64," in image_data:
                image_data = image_data.split("base64,", 1)[1]
        
        # Generate task ID (new-api-main compatible format)
        video_id = f"{model}-{uuid.uuid4().hex[:12]}"
        created_at = int(time.time())  # Unix timestamp in seconds
        
        # Async mode: create task and return immediately
        if async_mode:
            from ..core.database import Database
            from ..core.models import Task
            
            db = Database()
            
            # Create task in database
            task = Task(
                task_id=video_id,
                token_id=0,  # Will be set by generation handler
                model=final_model,
                prompt=prompt,
                status="in_progress",  # new-api-main uses in_progress
                progress=0.0
            )
            await db.create_task(task)
            
            # Store task info in memory for progress tracking
            _video_tasks[video_id] = {
                "id": video_id,
                "model": model,
                "internal_model": final_model,
                "prompt": prompt,
                "seconds": str(duration),
                "size": size,
                "image": image_data,
                "remix_target_id": remix_target_id,
                "style_id": style_id,
                "status": "in_progress",  # new-api-main compatible status
                "progress": 0,
                "created_at": created_at,
                "completed_at": None,
                "expires_at": None,
                "result_url": None,
                "error": None,
            }
            
            # Start background task
            asyncio.create_task(_process_video_generation_v2(video_id))
            
            # Return immediately with in_progress status (new-api-main compatible)
            # IMPORTANT: Must return 200 OK, not 201 Created - new-api checks for 200
            return JSONResponse(
                status_code=200,
                content={
                    "id": video_id,
                    "object": "video",
                    "model": model,
                    "status": "in_progress",
                    "progress": 0,
                    "created_at": created_at,
                    "seconds": str(duration),
                    "size": size,
                }
            )
        
        # Sync mode: wait for generation to complete
        chunks = []
        async for chunk in generation_handler.handle_generation(
            model=final_model,
            prompt=prompt,
            image=image_data,
            remix_target_id=remix_target_id,
            stream=True,
            style_id=style_id
        ):
            chunks.append(chunk)
        
        # Extract result
        video_info = _extract_video_info_from_chunks(chunks)
        url = video_info.get("url") or _extract_url_from_chunks(chunks)
        
        if url:
            # Return completed response (new-api-main compatible)
            # IMPORTANT: Must return 200 OK, not 201 Created - new-api checks for 200
            return JSONResponse(
                status_code=200,
                content={
                    "id": video_id,
                    "object": "video",
                    "model": model,
                    "status": "completed",
                    "progress": 100,
                    "created_at": created_at,
                    "completed_at": int(time.time()),
                    "seconds": str(duration),
                    "size": size,
                }
            )
        else:
            raise HTTPException(status_code=500, detail="Video generation failed")
    
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": str(e),
                    "code": "server_error"
                }
            }
        )


@router.get("/v1/videos/{video_id}")
async def get_video(
    video_id: str,
    api_key: str = Depends(verify_api_key_header)
):
    """Get video task status (new-api-main compatible)
    
    Returns the current status of a video generation task.
    Compatible with new-api-main sora2 relay format.
    
    **Response fields (new-api-main compatible):**
    - id: Video task ID
    - object: "video"
    - model: Model used for generation
    - status: "queued", "pending", "in_progress", "processing", "completed", "failed", "cancelled"
    - progress: Progress percentage (0-100)
    - created_at: Unix timestamp (seconds)
    - completed_at: Unix timestamp when completed (seconds, optional)
    - expires_at: Unix timestamp when expires (seconds, optional)
    - seconds: Video duration
    - size: Video resolution
    - remixed_from_video_id: Remix source video ID (optional)
    - error: Error details {message, code} (only when status="failed")
    - metadata: Extended metadata (optional)
    """
    # First check in-memory tasks
    task_info = _video_tasks.get(video_id)
    if task_info and isinstance(task_info, dict):
        # Debug log
        print(f"[GetVideo] {video_id}: Found in memory. status={task_info['status']}, progress={task_info['progress']}")
        
        # Build response (new-api-main compatible format)
        response = {
            "id": task_info["id"],
            "object": "video",
            "model": task_info["model"],
            "status": task_info["status"],  # Already using new-api-main compatible status
            "progress": task_info["progress"],
            "created_at": task_info["created_at"],
            "seconds": task_info["seconds"],
            "size": task_info["size"],
        }
        
        if task_info.get("completed_at"):
            response["completed_at"] = task_info["completed_at"]
        
        if task_info.get("expires_at"):
            response["expires_at"] = task_info["expires_at"]
        
        if task_info.get("remix_target_id"):
            response["remixed_from_video_id"] = task_info["remix_target_id"]
        
        if task_info.get("error"):
            response["error"] = task_info["error"]
        
        return JSONResponse(content=response)
    
    # Fallback to database
    from ..core.database import Database
    db = Database()
    
    task = await db.get_task(video_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    created_at = int(task.created_at.timestamp()) if task.created_at else int(time.time())
    
    # Map internal status to new-api-main compatible status
    status = task.status
    if status == "queued":
        status = "queued"
    elif status == "processing":
        status = "in_progress"
    elif status == "completed":
        status = "completed"
    elif status == "failed":
        status = "failed"
    
    # Extract model info from task
    model = "sora-2"
    if "sora2pro" in task.model or "sora-2-pro" in task.model:
        model = "sora-2-pro"
    
    # Extract duration and size
    duration = "15"
    if "10s" in task.model:
        duration = "10"
    elif "15s" in task.model:
        duration = "15"
    
    size = "720x1280"
    if "landscape" in task.model:
        size = "1280x720"
    
    response = {
        "id": video_id,
        "object": "video",
        "model": model,
        "status": status,
        "progress": int(task.progress) if task.progress else 0,
        "created_at": created_at,
        "seconds": duration,
        "size": size,
    }
    
    if task.status == "completed" and task.completed_at:
        response["completed_at"] = int(task.completed_at.timestamp())
    
    if task.error_message:
        response["error"] = {
            "message": task.error_message,
            "code": "generation_failed"
        }
    
    return JSONResponse(content=response)


@router.get("/v1/videos/{video_id}/content")
async def get_video_content(
    video_id: str,
    api_key: str = Depends(verify_api_key_header)
):
    """Get video content URL (returns direct video URL)
    
    Returns the video direct URL as plain text when the task is completed.
    Compatible with new-api-main sora2 relay format.
    """
    from fastapi.responses import PlainTextResponse
    
    # First check in-memory tasks
    task_info = _video_tasks.get(video_id)
    if task_info and isinstance(task_info, dict):
        if task_info["status"] != "completed":
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": f"Task not completed. Current status: {task_info['status']}",
                        "code": "task_not_completed"
                    }
                }
            )
        
        result_url = task_info.get("result_url")
        if not result_url:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "message": "Video content not available",
                        "code": "content_not_found"
                    }
                }
            )
        
        # Return direct URL as plain text
        return PlainTextResponse(content=result_url)
    
    # Fallback to database
    from ..core.database import Database
    db = Database()
    
    task = await db.get_task(video_id)
    if not task:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "message": "Task not found",
                    "code": "task_not_found"
                }
            }
        )
    
    if task.status == "failed":
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"Video generation failed: {task.error_message or 'Unknown error'}",
                    "code": "generation_failed"
                }
            }
        )
    
    if task.status != "completed" or not task.result_urls:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"Task not completed. Current status: {task.status}",
                    "code": "task_not_completed"
                }
            }
        )
    
    # Return direct URL as plain text
    return PlainTextResponse(content=task.result_urls)


# ============================================================
# /v1/videos/{video_id}/remix - Video Remix (new-api-main compatible)
# ============================================================

@router.post("/v1/videos/{video_id}/remix", status_code=200)
async def remix_video(
    video_id: str,
    request: Request,
    prompt: str = Form(None, description="Remix prompt"),
    model: str = Form("sora-2", description="Model ID: sora-2 or sora-2-pro"),
    seconds: Optional[str] = Form(None, description="Duration: '10' or '15'"),
    size: Optional[str] = Form(None, description="Output resolution"),
    style_id: Optional[str] = Form(None, description="Video style"),
    async_mode: Optional[bool] = Form(True, description="Async mode"),
    api_key: str = Depends(verify_api_key_header)
):
    """Remix an existing video (new-api-main compatible)
    
    Creates a new video based on an existing video with a new prompt.
    Compatible with new-api-main sora2 relay format.
    IMPORTANT: Returns 200 OK (not 201) for new-api compatibility.
    
    **Request:**
    - video_id: Source video ID to remix from
    - prompt: New prompt for the remix
    
    **Response format (new-api-main compatible):**
    ```json
    {
        "id": "sora-2-abc123def456",
        "object": "video",
        "model": "sora-2",
        "status": "in_progress",
        "progress": 0,
        "created_at": 1702388400,
        "remixed_from_video_id": "original-video-id"
    }
    ```
    """
    try:
        # Check if JSON body
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            prompt = body.get("prompt", prompt)
            model = body.get("model", model)
            seconds = body.get("seconds", seconds)
            size = body.get("size", size)
            style_id = body.get("style_id", style_id)
            async_mode = body.get("async_mode", async_mode)
        
        if not prompt:
            raise HTTPException(
                status_code=400, 
                detail={
                    "error": {
                        "message": "prompt is required",
                        "code": "invalid_request"
                    }
                }
            )
        
        # Normalize model name
        if model in ["sora", "sora-2", "sora2"]:
            model = "sora-2"
        elif model in ["sora-2-pro", "sora2-pro", "sora2pro"]:
            model = "sora-2-pro"
        
        # Default size
        if not size:
            size = "1280x720"
        
        # Parse seconds
        duration = 15
        if seconds:
            try:
                duration = int(seconds)
            except:
                duration = 15
        
        # Determine orientation from size
        try:
            width, height = map(int, size.split('x'))
            orient = "landscape" if width > height else "portrait"
        except:
            orient = "landscape"
        
        # Map to internal model
        final_model = f"sora-video-{orient}-{duration}s"
        
        # Generate new task ID
        new_video_id = f"{model}-{uuid.uuid4().hex[:12]}"
        created_at = int(time.time())
        
        if async_mode:
            from ..core.database import Database
            from ..core.models import Task
            
            db = Database()
            
            # Create task in database
            task = Task(
                task_id=new_video_id,
                token_id=0,
                model=final_model,
                prompt=prompt,
                status="in_progress",
                progress=0.0
            )
            await db.create_task(task)
            
            # Store task info with remix reference
            _video_tasks[new_video_id] = {
                "id": new_video_id,
                "model": model,
                "internal_model": final_model,
                "prompt": prompt,
                "seconds": str(duration),
                "size": size,
                "image": None,
                "remix_target_id": video_id,  # Original video ID
                "style_id": style_id,
                "status": "in_progress",
                "progress": 0,
                "created_at": created_at,
                "completed_at": None,
                "expires_at": None,
                "result_url": None,
                "error": None,
            }
            
            # Start background task
            asyncio.create_task(_process_video_generation_v2(new_video_id))
            
            # IMPORTANT: Must return 200 OK for new-api compatibility
            return JSONResponse(
                status_code=200,
                content={
                    "id": new_video_id,
                    "object": "video",
                    "model": model,
                    "status": "in_progress",
                    "progress": 0,
                    "created_at": created_at,
                    "seconds": str(duration),
                    "size": size,
                    "remixed_from_video_id": video_id,
                }
            )
        
        # Sync mode
        chunks = []
        async for chunk in generation_handler.handle_generation(
            model=final_model,
            prompt=prompt,
            remix_target_id=video_id,
            stream=True,
            style_id=style_id
        ):
            chunks.append(chunk)
        
        video_info = _extract_video_info_from_chunks(chunks)
        url = video_info.get("url") or _extract_url_from_chunks(chunks)
        
        if url:
            # IMPORTANT: Must return 200 OK for new-api compatibility
            return JSONResponse(
                status_code=200,
                content={
                    "id": new_video_id,
                    "object": "video",
                    "model": model,
                    "status": "completed",
                    "progress": 100,
                    "created_at": created_at,
                    "completed_at": int(time.time()),
                    "seconds": str(duration),
                    "size": size,
                    "remixed_from_video_id": video_id,
                }
            )
        else:
            raise HTTPException(status_code=500, detail="Video remix failed")
    
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": str(e),
                    "code": "server_error"
                }
            }
        )


# ============================================================
# /v1/test/video - Test Video Generation
# ============================================================

@router.post("/v1/test/video")
async def test_create_video(
    request: Request,
    prompt: str = Form(None, description="Video generation prompt (use @username to reference characters)"),
    model: str = Form("sora-video-10s", description="Model ID"),
    seconds: Optional[str] = Form(None, description="Duration: '10', '15', or '25'"),
    orientation: Optional[str] = Form(None, description="Orientation: 'landscape' or 'portrait'"),
    style_id: Optional[str] = Form(None, description="Video style: festive, retro, news, selfie, handheld, anime, comic, golden, vintage"),
    input_reference: Optional[UploadFile] = File(None, description="Reference image file for image-to-video"),
    input_image: Optional[str] = Form(None, description="Base64 encoded reference image for image-to-video"),
    remix_target_id: Optional[str] = Form(None, description="Sora share link video ID for remix (e.g., s_xxx)"),
    api_key: str = Depends(verify_api_key_header)
):
    """[TEST] Create video generation
    
    Same as /v1/videos but uses /nf/pending (v1) for polling instead of /nf/pending/v2.
    """
    try:
        # Check if JSON body
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            prompt = body.get("prompt", prompt)
            model = body.get("model", model)
            seconds = body.get("seconds", seconds)
            orientation = body.get("orientation", orientation)
            style_id = body.get("style_id", style_id)
            input_image = body.get("input_image", input_image)
            remix_target_id = body.get("remix_target_id", remix_target_id)
        
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        
        # Determine model from seconds/orientation
        final_model = model
        if seconds or orientation:
            duration = seconds or "10"
            orient = orientation or "landscape"
            if duration == "25":
                final_model = f"sora-video-{'portrait' if orient == 'portrait' else 'landscape'}-25s"
            elif duration == "15":
                final_model = f"sora-video-{'portrait' if orient == 'portrait' else 'landscape'}-15s"
            else:
                final_model = f"sora-video-{'portrait' if orient == 'portrait' else 'landscape'}-10s"
        
        # Validate model
        if final_model not in MODEL_CONFIG:
            raise HTTPException(status_code=400, detail=f"Invalid model: {final_model}")
        
        model_config = MODEL_CONFIG[final_model]
        if model_config["type"] != "video":
            raise HTTPException(status_code=400, detail=f"Model {final_model} is not a video model")
        
        # Process reference image for image-to-video
        image_data = None
        if input_reference:
            content = await input_reference.read()
            image_data = base64.b64encode(content).decode('utf-8')
        elif input_image:
            image_data = input_image
            if "base64," in image_data:
                image_data = image_data.split("base64,", 1)[1]
        
        # Non-streaming: collect all chunks and return final result
        # Use /nf/pending (v1) for polling in test endpoint
        chunks = []
        async for chunk in generation_handler.handle_generation(
            model=final_model,
            prompt=prompt,
            image=image_data,
            remix_target_id=remix_target_id,
            stream=True,  # Internal streaming
            style_id=style_id,
            use_pending_v1=True  # Use /nf/pending (v1) for polling
        ):
            chunks.append(chunk)
        
        # Extract final URL
        video_info = _extract_video_info_from_chunks(chunks)
        url = video_info.get("url") or _extract_url_from_chunks(chunks)
        permalink = video_info.get("permalink")
        if url:
            return JSONResponse(content={
                "id": f"video-{uuid.uuid4().hex[:24]}",
                "object": "video",
                "created": int(time.time()),
                "model": final_model,
                "data": [{"url": url, "permalink": permalink, "revised_prompt": prompt}]
            })
        else:
            raise HTTPException(status_code=500, detail="Video generation failed")
    
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": {"message": str(e), "type": "server_error", "param": None, "code": None}})


# ============================================================
# /v1/images/generations - Image Generation
# ============================================================

@router.post("/v1/images/generations")
async def create_image(
    request: Request,
    prompt: str = Form(None, description="Image generation prompt"),
    model: str = Form("sora-image", description="Model: sora-image, sora-image-landscape, sora-image-portrait"),
    n: int = Form(1, description="Number of images (currently only 1 supported)"),
    size: Optional[str] = Form(None, description="Image size (e.g., '1024x1024', '1792x1024', '1024x1792')"),
    quality: Optional[str] = Form("standard", description="Quality: standard or hd"),
    style: Optional[str] = Form(None, description="Style: natural or vivid"),
    response_format: Optional[str] = Form("url", description="Response format: url or b64_json"),
    input_reference: Optional[UploadFile] = File(None, description="Reference image file"),
    input_image: Optional[str] = Form(None, description="Base64 encoded reference image"),
    api_key: str = Depends(verify_api_key_header)
):
    """Create image generation (OpenAI-compatible)
    
    Supports both multipart/form-data and JSON body.
    Returns final result only (non-streaming output).
    
    **multipart/form-data example:**
    ```bash
    curl -X POST "https://your-domain.com/v1/images/generations" \\
      -H "Authorization: Bearer $API_KEY" \\
      -F prompt="A beautiful sunset over mountains" \\
      -F model="sora-image-landscape" \\
      -F size="1792x1024" \\
      -F input_reference="@reference.jpg;type=image/jpeg"
    ```
    
    **JSON body example:**
    ```json
    {
        "prompt": "A beautiful sunset over mountains",
        "model": "sora-image-landscape",
        "size": "1792x1024",
        "n": 1,
        "response_format": "url"
    }
    ```
    
    **Response:**
    ```json
    {
        "created": 1702388400,
        "data": [
            {
                "url": "https://...",
                "revised_prompt": "A beautiful sunset over mountains"
            }
        ]
    }
    ```
    """
    try:
        # Check if JSON body
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            prompt = body.get("prompt", prompt)
            model = body.get("model", model)
            n = body.get("n", n)
            size = body.get("size", size)
            quality = body.get("quality", quality)
            style = body.get("style", style)
            response_format = body.get("response_format", response_format)
            input_image = body.get("input_image", input_image)
        
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        
        # Map size to model
        final_model = model
        if size:
            try:
                width, height = map(int, size.split("x"))
                if width > height:
                    final_model = "sora-image-landscape"
                elif height > width:
                    final_model = "sora-image-portrait"
                else:
                    final_model = "sora-image"
            except (ValueError, AttributeError):
                pass
        
        # Validate model
        if final_model not in MODEL_CONFIG:
            raise HTTPException(status_code=400, detail=f"Invalid model: {final_model}")
        
        model_config = MODEL_CONFIG[final_model]
        if model_config["type"] != "image":
            raise HTTPException(status_code=400, detail=f"Model {final_model} is not an image model")
        
        # Process reference image
        image_data = None
        if input_reference:
            content = await input_reference.read()
            image_data = base64.b64encode(content).decode('utf-8')
        elif input_image:
            image_data = input_image
            if "base64," in image_data:
                image_data = image_data.split("base64,", 1)[1]
        
        # Generate image (internal streaming, external non-streaming)
        chunks = []
        async for chunk in generation_handler.handle_generation(
            model=final_model,
            prompt=prompt,
            image=image_data,
            stream=True
        ):
            chunks.append(chunk)
        
        # Extract final URL
        url = _extract_url_from_chunks(chunks)
        if not url:
            raise HTTPException(status_code=500, detail="Image generation failed")
        
        # OpenAI-compatible response
        return JSONResponse(content={
            "created": int(time.time()),
            "data": [
                {
                    "url": url,
                    "revised_prompt": prompt
                }
            ]
        })
    
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": {"message": str(e), "type": "server_error", "param": None, "code": None}})


# ============================================================
# /v1/characters - Character Creation
# ============================================================

@router.post("/v1/characters")
async def create_character(
    request: Request,
    model: str = Form("sora-video-10s", description="Video model to use"),
    video: Optional[UploadFile] = File(None, description="Video file for character extraction"),
    video_base64: Optional[str] = Form(None, description="Base64 encoded video"),
    timestamps: Optional[str] = Form(None, description="Video timestamps for character extraction (e.g., '0,3')"),
    username: Optional[str] = Form(None, description="Custom username for character"),
    display_name: Optional[str] = Form(None, description="Custom display name for character"),
    instruction_set: Optional[str] = Form(None, description="Character instruction set"),
    safety_instruction_set: Optional[str] = Form(None, description="Safety instruction set"),
    api_key: str = Depends(verify_api_key_header)
):
    """Create a character from video
    
    Supports both multipart/form-data and JSON body.
    Returns final result only (non-streaming output).
    
    **multipart/form-data example:**
    ```bash
    curl -X POST "https://your-domain.com/v1/characters" \\
      -H "Authorization: Bearer $API_KEY" \\
      -F model="sora-video-10s" \\
      -F video="@character_video.mp4;type=video/mp4" \\
      -F timestamps="0,3" \\
      -F username="my_character" \\
      -F display_name="My Character"
    ```
    
    **JSON body example:**
    ```json
    {
        "model": "sora-video-10s",
        "video": "base64_encoded_video_data",
        "timestamps": "0,3",
        "username": "my_character",
        "display_name": "My Character"
    }
    ```
    
    **Response:**
    ```json
    {
        "id": "char_xxxxxxxxxxxx",
        "object": "character",
        "created": 1702388400,
        "model": "sora-video-10s",
        "data": {
            "cameo_id": "ch_xxxxxxxxxxxx",
            "username": "my_character",
            "display_name": "My Character",
            "message": "Character created successfully"
        }
    }
    ```
    """
    try:
        # Check if JSON body
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            model = body.get("model", model)
            video_base64 = body.get("video", video_base64)
            timestamps = body.get("timestamps", timestamps)
            username = body.get("username", username)
            display_name = body.get("display_name", display_name)
            instruction_set = body.get("instruction_set", instruction_set)
            safety_instruction_set = body.get("safety_instruction_set", safety_instruction_set)
        
        # Validate model
        if model not in MODEL_CONFIG:
            raise HTTPException(status_code=400, detail=f"Invalid model: {model}")
        
        model_config = MODEL_CONFIG[model]
        if model_config["type"] != "video":
            raise HTTPException(status_code=400, detail=f"Model {model} is not a video model")
        
        # Process video
        video_data = None
        if video:
            content = await video.read()
            video_data = base64.b64encode(content).decode('utf-8')
        elif video_base64:
            video_data = video_base64
            if "base64," in video_data:
                video_data = video_data.split("base64,", 1)[1]
        
        if not video_data:
            raise HTTPException(status_code=400, detail="video is required for character creation")
        
        # Build character options
        character_options = CharacterOptions(
            timestamps=timestamps,
            username=username,
            display_name=display_name,
            instruction_set=instruction_set,
            safety_instruction_set=safety_instruction_set
        )
        
        # Create character (internal streaming, external non-streaming)
        chunks = []
        async for chunk in generation_handler.handle_generation(
            model=model,
            prompt="",  # Empty prompt for character creation only
            video=video_data,
            stream=True,
            character_options=character_options
        ):
            chunks.append(chunk)
        
        # Extract character info
        char_info = _extract_character_info(chunks)
        if not char_info:
            char_info = {"message": "Character creation completed"}
        
        # Add username/display_name to response if provided
        if username:
            char_info["username"] = username
        if display_name:
            char_info["display_name"] = display_name
        
        return JSONResponse(content={
            "id": f"char_{uuid.uuid4().hex[:24]}",
            "object": "character",
            "created": int(time.time()),
            "model": model,
            "data": char_info
        })
    
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": {"message": str(e), "type": "server_error", "param": None, "code": None}})
