"""OpenAI-compatible API endpoints for Sora generation

Provides standard OpenAI API format for:
- /v1/videos - Video generation (supports multipart/form-data and JSON)
- /v1/images/generations - Image generation (supports multipart/form-data and JSON)
- /v1/characters - Character creation (supports multipart/form-data and JSON)
"""
from fastapi import APIRouter, HTTPException, Depends, Form, File, UploadFile, Request
from fastapi.responses import JSONResponse
from typing import Optional, List, Union
from pydantic import BaseModel
import base64
import json
import time
import uuid
import re
from ..core.auth import verify_api_key_header
from ..services.generation_handler import GenerationHandler, MODEL_CONFIG
from ..core.models import CharacterOptions

router = APIRouter()

# Dependency injection
generation_handler: GenerationHandler = None


def set_generation_handler(handler: GenerationHandler):
    """Set generation handler instance"""
    global generation_handler
    generation_handler = handler


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
# /v1/videos - Video Generation
# ============================================================

@router.post("/v1/videos")
async def create_video(
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
    """Create video generation
    
    Supports both multipart/form-data and JSON body.
    Returns final result only (non-streaming output).
    
    To use a character, include @username in the prompt (e.g., "@my_cat walking in the park").
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
        chunks = []
        async for chunk in generation_handler.handle_generation(
            model=final_model,
            prompt=prompt,
            image=image_data,
            remix_target_id=remix_target_id,
            stream=True,  # Internal streaming
            style_id=style_id
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
