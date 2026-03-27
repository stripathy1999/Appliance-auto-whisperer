from pydantic import BaseModel, Field


class VideoHit(BaseModel):
    title: str = ""
    video_id: str = ""
    url: str = ""
    channel: str = ""
    score: float = 0.0
