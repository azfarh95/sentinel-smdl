"""Monkey-patch yt-dlp's Stripchat extractor.

Why this exists: as of 2026-05-11, Stripchat's __PRELOADED_STATE__ JSON
moved the model data from `viewCam.model` to `viewCamBase.model`.
yt-dlp 2026.03.17's StripchatIE._real_extract still reads `viewCam.model`,
which is now None — every probe returns "The channel is not currently
live" regardless of actual state.

This module rebuilds _real_extract with the corrected path. Falls back
to legacy `viewCam.model` for forward compatibility if Stripchat reverts.

Importing this module applies the patch as a side-effect — done once at
SMDL startup from main.py.

Remove this module entirely when yt-dlp's upstream extractor is fixed.
"""

from __future__ import annotations

import logging

from yt_dlp.extractor.stripchat import StripchatIE
from yt_dlp.utils import ExtractorError, UserNotLive, traverse_obj

logger = logging.getLogger(__name__)


def _patched_real_extract(self, url):
    video_id = self._match_id(url)
    webpage = self._download_webpage(
        url, video_id, headers=self.geo_verification_headers(),
    )

    data = self._search_json(
        r'<script\b[^>]*>\s*window\.__PRELOADED_STATE__\s*=',
        webpage, 'data', video_id,
    )

    # Stripchat now stores live model data under `viewCamBase.model`.
    # `viewCam.model` exists as a key but is None on current pages —
    # only contains UI-side state (autoResolution, isControlsBlockVisible
    # etc.). Try viewCamBase first, then viewCam as fallback.
    cam = (
        traverse_obj(data, ('viewCamBase', {dict}))
        or traverse_obj(data, ('viewCam', {dict}))
        or {}
    )

    if traverse_obj(cam, ('show', {dict})):
        raise ExtractorError('Model is in a private show', expected=True)

    model = traverse_obj(cam, ('model', {dict})) or {}
    if not model.get('isLive'):
        raise UserNotLive(video_id=video_id)

    model_id = model['id']
    # streamName is the canonical key in the URL template; usually equals
    # model.id but trust the JSON's value if present.
    stream_name = model.get('streamName') or model_id

    # Build host candidates. Current Stripchat structure (2026-05-11):
    #   configV3.initialCommon.hlsStreamHost          → primary (e.g. doppiocdn.org)
    #   configV3.initialCommon.defaultHlsStreamHost   → fallback (e.g. doppiocdn.com)
    #   configV3.initialCommon.hlsStreamHosts[A..E]   → regional CDN siblings
    # Plus the legacy upstream path for forward-compat if Stripchat reverts.
    hosts: list[str] = []
    ic = traverse_obj(data, ('configV3', 'initialCommon', {dict})) or {}
    for key in ('hlsStreamHost', 'defaultHlsStreamHost'):
        v = ic.get(key)
        if isinstance(v, str) and v and v not in hosts:
            hosts.append(v)
    regional = ic.get('hlsStreamHosts') or {}
    if isinstance(regional, dict):
        for v in regional.values():
            if isinstance(v, str) and v and v not in hosts:
                hosts.append(v)
    # Legacy structure (pre-2026-05): configV3.static.features...fallbackDomains[]
    for h in (traverse_obj(data, (
        (('config', 'data'), ('configV3', 'static')),
        (('features', 'featuresV2'), 'hlsFallback', 'fallbackDomains', ...),
        'hlsStreamHost',
    )) or []):
        if isinstance(h, str) and h and h not in hosts:
            hosts.append(h)

    formats = []
    for host in hosts:
        formats = self._extract_m3u8_formats(
            f'https://edge-hls.{host}/hls/{stream_name}/master/{stream_name}_auto.m3u8',
            video_id, ext='mp4', m3u8_id='hls', fatal=False, live=True,
        )
        if formats:
            break

    if not formats:
        self.raise_no_formats(
            f'Unable to extract stream — tried hosts: {hosts}',
            video_id=video_id,
        )

    return {
        'id':          video_id,
        'title':       video_id,
        'description': self._og_search_description(webpage),
        'is_live':     True,
        'formats':     formats,
        'age_limit':   18,
    }


def apply() -> None:
    """Replace StripchatIE._real_extract with the patched version. Idempotent."""
    if getattr(StripchatIE._real_extract, '__sentinel_patched__', False):
        return
    _patched_real_extract.__sentinel_patched__ = True
    StripchatIE._real_extract = _patched_real_extract
    logger.info("stripchat_patch: applied (viewCamBase.model path)")


# Apply on import so callers can just `import stripchat_patch` once.
apply()
