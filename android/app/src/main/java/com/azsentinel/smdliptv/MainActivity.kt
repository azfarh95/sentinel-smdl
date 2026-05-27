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
 *  - Loads https://media.az-sentinel.xyz/iptv
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
                    val isStreamUrl = urlStr.endsWith(".m3u8") ||
                                      urlStr.endsWith(".m3u")  ||
                                      urlStr.endsWith(".mpd")  ||
                                      urlStr.endsWith(".ts")
                    val isOurHost = host.endsWith("az-sentinel.xyz") ||
                                    host == "localhost"
                    if (isStreamUrl || !isOurHost) {
                        // External link or HLS stream — hand off to OS chooser
                        // so VLC / browser / etc. can claim it.
                        try {
                            val intent = Intent(Intent.ACTION_VIEW, url).apply {
                                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                            }
                            startActivity(intent)
                        } catch (_: Exception) {
                            // No handler — let the WebView try.
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

        // Optional fullscreen on Android 11+ (looks more like a "real" app).
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.setDecorFitsSystemWindows(false)
            window.insetsController?.let {
                it.hide(WindowInsets.Type.statusBars())
            }
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility =
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
                View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or
                View.SYSTEM_UI_FLAG_FULLSCREEN
        }

        if (savedInstanceState == null) {
            web.loadUrl("https://media.az-sentinel.xyz/iptv")
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
}
