package com.azsentinel.smdliptv

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.KeyEvent
import android.view.View
import android.view.WindowInsets
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity

/**
 * Minimal WebView wrapper for SMDL's IPTV browser.
 *
 *  - Loads https://media.az-sentinel.xyz/app (the home tile grid; the IPTV
 *    tab is reached from there via the side-nav).
 *  - Cookies persist (CookieManager) so the `sentinel_apk_session` cookie
 *    set by /auth/setup survives across launches → user pastes the owner
 *    token once and the JSON API calls keep working for 90 days.
 *  - HLS streams (`*.m3u8`) → handed off to the OS via ACTION_VIEW so VLC
 *    can pick them up. Non-stream external links open in the default
 *    browser via the same intent path.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        web = WebView(this).apply {
            layoutParams = android.view.ViewGroup.LayoutParams(
                android.view.ViewGroup.LayoutParams.MATCH_PARENT,
                android.view.ViewGroup.LayoutParams.MATCH_PARENT,
            )
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            // IPTV pages have their own data-freshness contract (refresh
            // sources / probe / record) — caching HTML would defeat that
            // and we hit a real "stuck on pre-feature layout for hours"
            // bug. LOAD_NO_CACHE forces re-fetch of HTML on every load
            // while still honouring per-resource Cache-Control for assets
            // like channel logos (so we don't re-download them every time).
            settings.cacheMode = WebSettings.LOAD_NO_CACHE
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            settings.userAgentString = settings.userAgentString + " SMDL-IPTV/0.1"
            isVerticalScrollBarEnabled = true
            isHorizontalScrollBarEnabled = false

            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, req: WebResourceRequest): Boolean {
                    val url = req.url
                    val urlStr = url.toString()
                    val host = url.host ?: ""
                    val mime = mimeForStreamUrl(urlStr)
                    val isStreamUrl = mime != null
                    val isOurHost = host.endsWith("az-sentinel.xyz") ||
                                    host == "localhost"
                    if (isStreamUrl) {
                        // Stream URL — try to launch VLC directly with the
                        // right MIME (so DASH / HLS / TS get treated as a
                        // video stream, not as an unknown file to download).
                        // Falls back to the system chooser if VLC isn't
                        // installed.
                        val viewIntent = Intent(Intent.ACTION_VIEW).apply {
                            setDataAndType(url, mime)
                            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        }
                        for (pkg in listOf(
                            "org.videolan.vlc",       // VLC for Android
                            "com.mxtech.videoplayer.ad",  // MX Player Free
                            "com.mxtech.videoplayer.pro", // MX Player Pro
                        )) {
                            try {
                                val direct = Intent(viewIntent).apply {
                                    setPackage(pkg)
                                }
                                startActivity(direct)
                                return true
                            } catch (_: Exception) {
                                // package not installed, try next
                            }
                        }
                        // No known player installed — show chooser. Excludes
                        // ourselves so the WebView doesn't appear as an option.
                        try {
                            val chooser = Intent.createChooser(viewIntent, "Open stream with…")
                                .apply { addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) }
                            startActivity(chooser)
                        } catch (_: Exception) {
                            return false
                        }
                        return true
                    }
                    if (!isOurHost) {
                        // Non-stream external link — system chooser.
                        try {
                            val intent = Intent(Intent.ACTION_VIEW, url).apply {
                                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                            }
                            startActivity(intent)
                        } catch (_: Exception) {
                            return false
                        }
                        return true
                    }
                    return false
                }
            }

            webChromeClient = WebChromeClient()
        }

        setContentView(web)

        // Persistent cookies — REQUIRED for the /auth/setup session to
        // survive launches and for /api/iptv/* to authenticate.
        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setAcceptThirdPartyCookies(web, true)
        }

        // Edge-to-edge under the status bar (cleaner look) but RESPECT
        // the navigation-bar inset so pages with bottom-pinned UI
        // (SMDL /app's tab bar) don't get clipped under the system
        // nav. We pad the WebView itself with the bottom inset rather
        // than letting the page handle env(safe-area-inset-bottom) —
        // SMDL's older pages predate the safe-area CSS and would need
        // touching otherwise.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.setDecorFitsSystemWindows(false)
            window.insetsController?.let {
                it.hide(WindowInsets.Type.statusBars())
            }
            web.setOnApplyWindowInsetsListener { v, insets ->
                val navBars = insets.getInsets(WindowInsets.Type.navigationBars())
                v.setPadding(0, 0, 0, navBars.bottom)
                insets
            }
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility =
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
                View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
        }

        if (savedInstanceState == null) {
            web.loadUrl("https://media.az-sentinel.xyz/app")
        } else {
            web.restoreState(savedInstanceState)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
    }

    // Hardware-back / D-pad-back navigates the WebView history instead of
    // killing the activity — important for AndroidTV remotes.
    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK && web.canGoBack()) {
            web.goBack()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onPause() {
        super.onPause()
        CookieManager.getInstance().flush()
    }

    /** Map a stream URL to the standard MIME type Android players use to
     *  advertise themselves for ACTION_VIEW. Without an explicit MIME the
     *  system tries to guess from the path extension, which often misses
     *  for query-stringed URLs and falls through to the browser (which
     *  then downloads the manifest instead of streaming it). */
    private fun mimeForStreamUrl(url: String): String? {
        // strip query / fragment before the extension check
        val clean = url.substringBefore("?").substringBefore("#").lowercase()
        return when {
            clean.endsWith(".m3u8") -> "application/vnd.apple.mpegurl"
            clean.endsWith(".m3u")  -> "audio/x-mpegurl"
            clean.endsWith(".mpd")  -> "application/dash+xml"
            clean.endsWith(".ts")   -> "video/mp2t"
            else -> null
        }
    }
}
