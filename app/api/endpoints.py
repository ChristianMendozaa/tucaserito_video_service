import uuid
from typing import List
import logging
import urllib.parse
import httpx
import asyncio
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, status, Depends, Request

from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from cachetools import TTLCache
import time

from app.api.auth_deps import get_current_user_id
from app.core.rate_limit import limiter

from app.services.vertex_service import generate_video_async, extend_video_async, get_operation_status
from app.services.gcs_service import get_output_uri, generate_signed_url, get_bucket
from app.services.firestore_service import create_video_job, get_video_job, update_video_job, list_video_jobs, list_video_jobs_by_user
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBearer()

# Cache to avoid spamming the subscription service on burst requests
subs_cache = TTLCache(maxsize=1000, ttl=30)

# Locks in-memory to prevent concurrency race conditions on quota deduction
status_locks = {}

def get_video_lock(video_id: str) -> asyncio.Lock:
    if video_id not in status_locks:
        status_locks[video_id] = asyncio.Lock()
    return status_locks[video_id]

async def verify_can_generate(token: str):
    """Verifica si el plan permite generar videos (sin descontar cuota). Con Caché de 30s."""
    if token in subs_cache:
        data = subs_cache[token]
    else:
        url = f"{settings.SUBSCRIPTION_SERVICE_URL}/api/v1/subscriptions/me"
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                subs_cache[token] = data
            else:
                raise HTTPException(status_code=403, detail="No autorizado o sin plan activo.")

    if not data.get("can_generate_video", False) or data.get("videos_remaining", 0) <= 0:
        raise HTTPException(status_code=409, detail="Cuota de video agotada o plan inactivo.")

async def consume_quota_s2s(user_id: str):
    """Descuenta la cuota vía admin S2S (llamado solo cuando el video se completa exitosamente)."""
    admin_key = settings.SUBSCRIPTION_ADMIN_API_KEY
    url = f"{settings.SUBSCRIPTION_SERVICE_URL}/api/v1/admin/subscriptions/{user_id}/consume-video"
    headers = {"X-Admin-Key": admin_key}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(url, headers=headers)
            logger.info(f"Cuota S2S descontada para user {user_id}")
        except Exception as e:
            logger.error(f"Error al descontar cuota S2S: {e}")


class VideoGenerateResponse(BaseModel):
    video_id: str
    status: str

@router.post("/generate", response_model=VideoGenerateResponse, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("5/minute")
async def generate_video(
    request: Request,
    images: List[UploadFile] = File(...),
    prompt_veo_visual: str = Form(...),
    prompt_veo_audio: str = Form(""),
    aspect_ratio: str = Form("16:9"),
    script_text: str = Form(""),
    user_id: str = Depends(get_current_user_id),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    try:
        if len(images) > 3:
            raise HTTPException(status_code=400, detail="No se permiten más de 3 imágenes por solicitud para generar videos.")
            
        # 1. Verificar elegibilidad primero sin descontar
        token = credentials.credentials
        await verify_can_generate(token)

            
        video_id = str(uuid.uuid4())
        # We give Veo a directory prefix to output the generated video utilizing our local UUID
        output_uri = get_output_uri(video_id)

        # Veo online prediction takes a single base image. We use the first one provided.
        image_bytes = await images[0].read()
        mime_type = images[0].content_type or "image/jpeg"
        
        operation_name = await generate_video_async(
            image_bytes=image_bytes,
            prompt_visual=prompt_veo_visual,
            prompt_audio=prompt_veo_audio,
            duration_seconds=8,
            aspect_ratio=aspect_ratio,
            output_uri=output_uri,
            mime_type=mime_type
        )
        
        metadata = {
            "prompt_visual": prompt_veo_visual,
            "prompt_audio": prompt_veo_audio,
            "duration": 8,
            "aspect_ratio": aspect_ratio,
            "script_text": script_text
        }
        
        create_video_job(video_id, operation_name, metadata, user_id)
        
        logger.info(f"Started video generation: {video_id} (Operation: {operation_name})")
        
        return VideoGenerateResponse(
            video_id=video_id,
            status="PROCESSING"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting video generation: {e}")
        # Ya no devolvemos cuota porque nunca la consumimos
        raise HTTPException(status_code=500, detail=str(e))

class VideoExtendRequest(BaseModel):
    video_id: str
    prompt_veo_visual: str
    prompt_veo_audio: str = ""
    script_text: str = ""

@router.post("/extend", response_model=VideoGenerateResponse, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("5/minute")
async def extend_video(req: VideoExtendRequest, request: Request, user_id: str = Depends(get_current_user_id), credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        # Verify the original video exists and is completed
        job = get_video_job(req.video_id)
        if not job or job.get("status") != "COMPLETED":
            raise HTTPException(status_code=400, detail="Original video not found or not completed.")
            
        # 1. Verificar elegibilidad primero
        token = credentials.credentials
        await verify_can_generate(token)

            
        new_video_id = str(uuid.uuid4())
        
        # Get the actual GCS URI from the completed job metadata, fallback if legacy
        original_gcs_uri = job.get("gcs_uri")
        if not original_gcs_uri:
            original_gcs_uri = f"gs://{settings.GCS_BUCKET_NAME}/videos/{req.video_id}/video.mp4"
        
        new_output_uri = get_output_uri(new_video_id)
        
        original_aspect_ratio = job.get("metadata", {}).get("aspect_ratio", "16:9")
        
        operation_name = await extend_video_async(
            video_uri=original_gcs_uri,
            prompt_visual=req.prompt_veo_visual,
            prompt_audio=req.prompt_veo_audio,
            output_uri=new_output_uri,
            duration_seconds=7,
            aspect_ratio=original_aspect_ratio
        )
        
        metadata = {
            "type": "extension",
            "original_video_id": req.video_id,
            "prompt_visual": req.prompt_veo_visual,
            "duration": 7,
            "aspect_ratio": original_aspect_ratio,
            "script_text": req.script_text
        }
        
        create_video_job(new_video_id, operation_name, metadata, user_id)
        
        logger.info(f"Started video extension: {new_video_id} from {req.video_id}")
        
        return VideoGenerateResponse(
            video_id=new_video_id,
            status="PROCESSING"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting video extension: {e}")
        # Ya no devolvemos cuota porque nunca la descontamos
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/list")
@limiter.limit("30/minute")
async def get_all_videos(request: Request):
    try:
        jobs = list_video_jobs()
        updated_jobs = []
        
        for job in jobs:
            # Auto-heal jobs that are stuck in PROCESSING state
            if job.get("status") == "PROCESSING":
                video_id = job.get("video_id")
                try:
                    # get_video_status will automatically update Firestore if it finished
                    live_status = await get_video_status(video_id, request)
                    job.update(live_status)
                except Exception as ex:
                    logger.warning(f"Error auto-updating status for {video_id}: {ex}")
            # Ensure proper typing for datetime serialization, just in case firestore outputs DatetimeWithNanoseconds
            updated_jobs.append(job)
            
        return {"videos": updated_jobs}
    except Exception as e:
        logger.error(f"Error listing video jobs: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/my-videos")
@limiter.limit("30/minute")
async def get_user_videos(request: Request, user_id: str = Depends(get_current_user_id)):
    try:
        jobs = list_video_jobs_by_user(user_id)
        updated_jobs = []
        
        for job in jobs:
            # Auto-heal jobs that are stuck in PROCESSING state
            if job.get("status") == "PROCESSING":
                video_id = job.get("video_id")
                try:
                    # get_video_status will automatically update Firestore if it finished
                    live_status = await get_video_status(video_id, request)
                    job.update(live_status)
                except Exception as ex:
                    logger.warning(f"Error auto-updating status for {video_id}: {ex}")
            # Ensure proper typing for datetime serialization, just in case firestore outputs DatetimeWithNanoseconds
            updated_jobs.append(job)
            
        return {"videos": updated_jobs}
    except Exception as e:
        logger.error(f"Error listing user video jobs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{video_id:path}")
@limiter.limit("60/minute")
async def get_video_status(video_id: str, request: Request):
    try:
        # Check Firestore first
        job = get_video_job(video_id)
        if not job:
            raise HTTPException(status_code=404, detail="Video job not found.")
            
        current_status = job.get("status")
        
        if current_status == "COMPLETED":
            return {
                "video_id": video_id,
                "status": "COMPLETED",
                "video_url": job.get("final_url")
            }
        elif current_status == "FAILED":
            return {
                "video_id": video_id,
                "status": "FAILED",
                "error": job.get("error")
            }
            
        # If PROCESSING, get the underlying Vertex AI operation id
        operation_id = job.get("operation_id")
        if not operation_id:
            return job # Safety fallback if operation_id is missing
            
        decoded_op_id = urllib.parse.unquote(operation_id)
        
        op_status = await get_operation_status(decoded_op_id)
        is_done = op_status.get("done", False)
        
        if is_done:
            if "error" in op_status:
                err_obj = op_status["error"]
                err_msg = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                
                update_video_job(video_id, {
                    "status": "FAILED",
                    "error": err_msg
                })
                return {
                    "video_id": video_id,
                    "status": "FAILED",
                    "error": err_msg
                }
            
            # The operation is done and successful. Find the MP4 in the bucket.
            bucket = get_bucket()
            prefix = f"videos/{video_id}/"
            blobs = list(bucket.list_blobs(prefix=prefix))
            
            video_url = None
            gcs_uri = None
            if blobs:
                mp4_blobs = [b for b in blobs if b.name.endswith(".mp4")]
                if mp4_blobs:
                    video_blob = mp4_blobs[0]
                    video_url = generate_signed_url(video_blob.name)
                    gcs_uri = f"gs://{settings.GCS_BUCKET_NAME}/{video_blob.name}"
                else:
                    video_url = generate_signed_url(blobs[0].name)
                    gcs_uri = f"gs://{settings.GCS_BUCKET_NAME}/{blobs[0].name}"
            
            # If not in the bucket, Veo returns the video as Base64 in the response body!
            if not video_url:
                import base64
                
                def find_base64_in_dict(obj):
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if isinstance(v, str) and len(v) > 500 and "mimeType" not in k:
                                return v
                            res = find_base64_in_dict(v)
                            if res: return res
                    elif isinstance(obj, list):
                        for item in obj:
                            res = find_base64_in_dict(item)
                            if res: return res
                    return None
                
                video_b64 = find_base64_in_dict(op_status)
                if video_b64:
                    try:
                        logger.info(f"Uploading base64 fallback video to GCS for {video_id}")
                        video_bytes = base64.b64decode(video_b64)
                        blob_name = f"{prefix}video.mp4"
                        blob = bucket.blob(blob_name)
                        blob.upload_from_string(video_bytes, content_type="video/mp4")
                        video_url = generate_signed_url(blob_name)
                        gcs_uri = f"gs://{settings.GCS_BUCKET_NAME}/{blob_name}"
                    except Exception as upload_err:
                        logger.error(f"Failed to upload base64 video to GCS: {upload_err}")
                
            if not video_url:
                err_msg = "El video no pudo generarse. Vertex AI puede haber bloqueado el contenido por filtros de seguridad."
                resp = op_status.get("response", {})
                if isinstance(resp, dict):
                    # Sometimes Vertex returns safety ratings or block reasons inside the response payload
                    if "error" in resp:
                        err_msg = f"Error Vertex AI: {resp.get('error')}"
                    elif "blockReason" in resp:
                        err_msg = f"Filtro de Seguridad Vertex AI: {resp.get('blockReason')}"
                    else:
                        # Append stringified payload for debugging just in case
                        safe_resp = {k: v for k, v in resp.items() if "bytesBase64Encoded" not in str(v)}
                        err_msg += f" Detalles: {safe_resp}"
                        
                update_video_job(video_id, {
                    "status": "FAILED",
                    "error": err_msg
                })
                return {
                    "video_id": video_id,
                    "status": "FAILED",
                    "error": err_msg
                }

            # Update Firestore with completion using a Lock to prevent Race Conditions
            async with get_video_lock(video_id):
                # Re-fetch job inside lock to ensure no other concurrent request already processed it
                latest_job = get_video_job(video_id)
                if not latest_job:
                    raise HTTPException(status_code=404, detail="Job disappeared.")
                
                if latest_job.get("status") == "COMPLETED":
                    return {
                        "video_id": video_id,
                        "status": "COMPLETED",
                        "video_url": latest_job.get("final_url"),
                        "raw_response": op_status.get("response", {})
                    }

                user_id = latest_job.get("user_id")
                quota_consumed = latest_job.get("quota_consumed", False)
                
                update_data = {
                    "status": "COMPLETED",
                    "final_url": video_url,
                    "gcs_uri": gcs_uri
                }

                if not quota_consumed and user_id:
                    try:
                        await consume_quota_s2s(user_id)
                        update_data["quota_consumed"] = True
                    except Exception as e:
                        logger.error(f"Failed to consume quota for {video_id}: {e}")
                        # If quota deduction fails, we still mark it as completed but leave quota_consumed as False / mark it for retry
                
                update_video_job(video_id, update_data)

            return {
                "video_id": video_id,
                "status": "COMPLETED",
                "video_url": video_url,
                "raw_response": op_status.get("response", {})
            }
        else:
            return {
                "video_id": video_id,
                "status": "PROCESSING",
                "progress": op_status.get("metadata", {})
            }
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting video status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

