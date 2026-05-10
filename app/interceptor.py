"""URL detection — regex patterns for supported video platforms."""

import re

_PATTERNS: dict[str, list[str]] = {
    "instagram": [
        r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv|stories)/[\w\.\-]+(?:/\d+)?",
        r"https?://(?:www\.)?instagram\.com/[\w\.]+/?\s*$",
    ],
    "tiktok": [
        r"https?://(?:www\.)?tiktok\.com/@[\w\-\.]+/video/\d+",
        r"https?://(?:vm|vt)\.tiktok\.com/[\w\-]+",
    ],
    "youtube": [
        r"https?://(?:www\.)?youtube\.com/shorts/[\w\-]+",
        r"https?://(?:www\.)?youtube\.com/watch\?v=[\w\-]+",
        r"https?://youtu\.be/[\w\-]+",
    ],
    "twitter": [
        r"https?://(?:www\.)?(?:twitter|x)\.com/\w+/status/\d+",
    ],
    "facebook": [
        r"https?://(?:www\.)?facebook\.com/(?:watch|video)\.php\?v=\d+",
        r"https?://fb\.watch/[\w\-]+",
    ],
    "reddit": [
        r"https?://(?:www\.)?reddit\.com/r/\w+/comments/[\w\-]+",
        r"https?://v\.redd\.it/[\w\-]+",
    ],
    "bilibili": [
        r"https?://(?:www\.)?bilibili\.com/video/[\w\-]+",
        r"https?://b23\.tv/[\w\-]+",
    ],
    "pinterest": [
        r"https?://(?:www\.)?pinterest\.com/pin/\d+",
    ],
    # Live-capable platforms (live recording handled by live_downloader.py).
    # These broad patterns match channels, VODs, and clips — yt-dlp resolves
    # which it is + whether it's currently live during identify_post.
    "twitch": [
        r"https?://(?:www\.|m\.|clips\.)?twitch\.tv/[\w\-/]+",
    ],
    "kick": [
        r"https?://(?:www\.)?kick\.com/[\w\-/]+",
    ],
    "chaturbate": [
        r"https?://(?:www\.)?chaturbate\.com/[\w\-]+/?",
    ],
    "stripchat": [
        r"https?://(?:www\.)?stripchat\.com/[\w\-]+/?",
    ],
    "cam4": [
        r"https?://(?:www\.)?cam4\.com/[\w\-]+/?",
    ],
    "bongacams": [
        r"https?://(?:www\.)?bongacams\.(?:com|net)/(?:profile/)?[\w\-]+/?",
    ],
    # xhamsterlive is a re-skin of stripchat sharing the same backend; rooms
    # map 1:1. yt-dlp doesn't have a dedicated xhamsterlive extractor, so we
    # detect the URL and rewrite to stripchat.com before yt-dlp ever sees it.
    "xhamsterlive": [
        r"https?://(?:www\.)?xhamsterlive\.com/[\w\-]+/?",
    ],
}

# Some sites are mirror/skin redirects of an upstream that yt-dlp DOES
# support. Rewrite them at detection time. Format: (matched_platform,
# rewritten_platform, sed-style (old, new) host swap).
_REWRITES: dict[str, tuple[str, str, str]] = {
    "xhamsterlive": ("stripchat", "xhamsterlive.com", "stripchat.com"),
}

_COMPILED = [
    (platform, re.compile(pattern, re.IGNORECASE))
    for platform, patterns in _PATTERNS.items()
    for pattern in patterns
]


def find_video_url(text: str) -> tuple[str, str] | None:
    """Return (platform, url) for the first video URL found, or None.

    For mirror sites listed in _REWRITES, the URL is rewritten to its
    canonical form so that yt-dlp's existing extractor matches.
    """
    for platform, regex in _COMPILED:
        m = regex.search(text)
        if m:
            url = m.group(0)
            if platform in _REWRITES:
                new_platform, old_host, new_host = _REWRITES[platform]
                url = url.replace(old_host, new_host)
                platform = new_platform
            return platform, url
    return None
