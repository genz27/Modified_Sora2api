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
            except:
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
            except:
                pass
    return result


# ============================================================
# /v1/videos - Video Generation (OpenAI Sora Compatible)
# ============================================================

@router.post("/v1/videos", status_code=201)
async def create_video(
    request: Request,
    prompt: str = Form(None, description="Video generation prompt"),
    model: str = Form("sora-video-landscape-10s", description="Model ID"),
    seconds: Optional[str] = Form(None, description="Duration: '10', '15', or '25'"),
    size: Optional[str] = Form(None, description="Output resolution (e.g., '1920x1080', '1080x1920')"),
    orientation: Optional[str] = Form(None, description="Orientation: 'landscape' or 'portrait'"),
    style_id: Optional[str] = Form(None, description="Video style"),
    input_reference: Optional[UploadFile] = File(None, description="Reference image file"),
    input_image: Optional[str] = Form(None, description="Base64 encoded reference image"),
    remix_target_id: Optional[str] = Form(None, description="Remix target video ID"),
    metadata: Optional[str] = Form(None, description="Extended parameters (JSON string)"),
    api_key: str = Depends(verify_api_key_header)
):
    """Create video generation (OpenAI Sora Compatible)
    
    Supports both multipart/form-data and JSON body.
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
            remix_target_id = body.get("remix_target_id", remix_target_id)
            metadata = body.get("metadata", metadata)
        
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        
        # Parse size to determine orientation
        if size and not orientation:
            try:
                width, height = map(int, size.lower().replace('*', 'x').split('x'))
                orientation = "portrait" if height > width else "landscape"
            except:
                orientation = "landscape"
        
        # Default values
        duration = seconds or "10"
        orient = orientation or "landscape"
        
        # Determine final model
        final_model = model
        if model in ["sora-2", "sora"]:
            # Map sora-2 to internal model
            if duration == "25":
                final_model = f"sora-video-{orient}-25s"
            elif duration == "15":
                final_model = f"sora-video-{orient}-15s"
            else:
                final_model = f"sora-video-{orient}-10s"
        elif seconds or orientation:
            if duration == "25":
                final_model = f"sora-video-{orient}-25s"
            elif duration == "15":
                final_model = f"sora-video-{orient}-15s"
            else:
                final_model = f"sora-video-{orient}-10s"
        
        # Validate model
        if final_model not in MODEL_CONFIG:
            raise HTTPException(status_code=400, detail=f"Invalid model: {final_model}")
        
        model_config = MODEL_CONFIG[final_model]
        if model_config["type"] != "video":
            raise HTTPException(status_code=400, detail=f"Model {final_model} is not a video model")
        
        # Process reference image
        image_data = None
        if input_reference:
            content = await input_reference.read()
            image_data = base64.b64encode(content).decode('utf-8')
        elif input_image:
            image_data = input_image
            if "base64," in image_data:
                image_data = image_data.split("base64,", 1)[1]
        
        # Generate video
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
        permalink = video_info.get("permalink")
        
        video_id = f"video_{uuid.uuid4().hex[:24]}"
        created_at = int(time.time())
        
        if url:
            # Return OpenAI Sora compatible response
            return JSONResponse(
                status_code=201,
                content={
                    "id": video_id,
                    "object": "video",
                    "model": model,
                    "created_at": created_at,
                    "status": "succeeded",
                    "progress": 100,
                    "expires_at": created_at + 86400,  # 24 hours
                    "size": size or f"{model_config.get('width', 1920)}x{model_config.get('height', 1080)}",
                    "seconds": duration,
                    "quality": "standard",
                    "url": url,
                    "permalink": permalink
                }
            )
        else:
            raise HTTPException(status_code=500, detail="Video generation failed")
    
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "server_error"}}
        )


@router.get("/v1/videos/{video_id}")
async def get_video(
    video_id: str,
    api_key: str = Depends(verify_api_key_header)
):
    """Get video task status (OpenAI Sora Compatible)"""
    from ..core.database import Database
    db = Database()
    
    # Try to get task from database
    task = await db.get_task(video_id)
    if not task:
        raise HTTPException(status_code=404, detail="Video not found")
    
    created_at = int(task.created_at.timestamp()) if task.created_at else int(time.time())
    
    response = {
        "id": video_id,
        "object": "video",
        "model": task.model,
        "created_at": created_at,
        "status": "succeeded" if task.status == "completed" else ("failed" if task.status == "failed" else "processing"),
        "progress": int(task.progress) if task.progress else 0,
        "expires_at": created_at + 86400,
        "size": "1920x1080",
        "seconds": "10",
        "quality": "standard",
        "remixed_from_video_id": None,
        "error": {"message": task.error_message} if task.error_message else None
    }
    
    if task.result_urls:
        response["url"] = task.result_urls
    
    return JSONResponse(content=response)


@router.get("/v1/videos/{video_id}/content")
async def get_video_content(
    video_id: str,
    variant: Optional[str] = None,
    api_key: str = Depends(verify_api_key_header)
):
    """Download video content (OpenAI Sora Compatible)"""
    from ..core.database import Database
    import httpx
    
    db = Database()
    task = await db.get_task(video_id)
    
    if not task:
        raise HTTPException(status_code=404, detail="Video not found")
    
    if task.status != "completed" or not task.result_urls:
        raise HTTPException(status_code=400, detail="Video not ready for download")
    
    # Redirect to video URL
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=task.result_urls, status_code=302)


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
            except:
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
