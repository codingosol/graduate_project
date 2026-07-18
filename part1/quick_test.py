import json
import os
import sys

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

API_KEY = os.environ.get("YOUTUBE_API_KEY")
if not API_KEY:
    raise SystemExit("YOUTUBE_API_KEY가 .env에 없습니다. part1/.env 파일에 추가해주세요.")

youtube = build("youtube", "v3", developerKey=API_KEY)

CHANNEL_HANDLE = "@ytnnews24"  # 테스트용 채널 (필요하면 다른 채널로 교체)

# 1. 채널 정보 + 업로드 재생목록 ID
ch_resp = youtube.channels().list(part="snippet,contentDetails", forHandle=CHANNEL_HANDLE).execute()
channel = ch_resp["items"][0]
uploads_playlist_id = channel["contentDetails"]["relatedPlaylists"]["uploads"]
print(f"채널명: {channel['snippet']['title']}")
print(f"업로드 재생목록 ID: {uploads_playlist_id}\n")

# 2. 최근 영상 5개
pl_resp = youtube.playlistItems().list(
    part="snippet", playlistId=uploads_playlist_id, maxResults=5
).execute()
videos = pl_resp["items"]
print("최근 영상 5개:")
for v in videos:
    print(f"- [{v['snippet']['resourceId']['videoId']}] {v['snippet']['title']}")

# 3. 댓글이 열려있는 첫 영상의 인기 댓글 5개 (order=relevance)
print()
for v in videos:
    video_id = v["snippet"]["resourceId"]["videoId"]
    try:
        ct_resp = youtube.commentThreads().list(
            part="snippet", videoId=video_id, order="relevance", maxResults=5
        ).execute()
    except HttpError as e:
        reason = json.loads(e.content)["error"]["errors"][0]["reason"]
        print(f"[{video_id}] 댓글 조회 실패 ({reason}) - 다음 영상 시도")
        continue

    print(f"'{v['snippet']['title']}' 인기 댓글 5개:")
    for item in ct_resp["items"]:
        c = item["snippet"]["topLevelComment"]["snippet"]
        print(f"- [좋아요 {c['likeCount']}] {c['textDisplay']}")
    break
else:
    print("가져온 영상 5개 모두 댓글 조회 실패")
