package com.natantec.tvcast;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONObject;

import java.util.regex.Pattern;

/**
 * TV Cast (TV): WebView em tela cheia.
 * Tela de IP -> abre http://IP:8765/tv, que o servidor do PC controla.
 * Uma segunda WebView (pageView) fica por cima quando o PC manda mostrar uma URL,
 * com o CSS personalizado injetado.
 */
public class MainActivity extends Activity {

    private static final String SETUP_URL = "file:///android_asset/setup.html";
    private static final Pattern HOST = Pattern.compile("^[A-Za-z0-9.\\-]+(:\\d{1,5})?$");

    private WebView web;        // página do TV Cast (controlada pelo PC)
    private WebView pageView;   // site mostrado por cima, sem a ponte Android
    private String pageCss = "";
    private SharedPreferences prefs;
    private boolean onSetup = true;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        prefs = getSharedPreferences("tvcast", MODE_PRIVATE);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        pageView = new WebView(this);
        pageView.setBackgroundColor(Color.BLACK);
        pageView.setVisibility(View.GONE);
        root.addView(pageView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        baseSettings(web);
        web.getSettings().setCacheMode(WebSettings.LOAD_NO_CACHE);
        web.getSettings().setAllowFileAccess(true);
        web.addJavascriptInterface(new Bridge(), "Android");
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame() && !onSetup) {
                    onSetup = true;
                    hidePage();
                    v.loadUrl(SETUP_URL + "?err=1");
                }
            }
        });

        // Site externo: sem a ponte Android (não confiamos no conteúdo).
        baseSettings(pageView);
        pageView.setWebChromeClient(new WebChromeClient());
        pageView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView v, String url) {
                injectCss();
            }

            @Override
            public void onPageCommitVisible(WebView v, String url) {
                injectCss();
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) {
                    v.loadData("<body style='background:#000;color:#fff;font:32px sans-serif;"
                            + "text-align:center;padding-top:20%'>N&atilde;o foi poss&iacute;vel abrir a p&aacute;gina."
                            + "</body>", "text/html; charset=UTF-8", null);
                }
            }
        });

        String ip = prefs.getString("ip", "");
        if (ip.length() > 0) {
            openTv(ip);
        } else {
            openSetup();
        }
    }

    private void baseSettings(WebView w) {
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
    }

    private void openSetup() {
        onSetup = true;
        hidePage();
        web.loadUrl(SETUP_URL);
    }

    private void openTv(String hostPort) {
        onSetup = false;
        String h = hostPort.contains(":") ? hostPort : hostPort + ":8765";
        web.loadUrl("http://" + h + "/tv");
    }

    private void hidePage() {
        pageView.loadUrl("about:blank");
        pageView.setVisibility(View.GONE);
    }

    /** Mantém o CSS personalizado no site (e reaplica se o site recriar o <head>). */
    private void injectCss() {
        String q = JSONObject.quote(pageCss).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029");
        String js = "(function(){window.__tvcss=" + q + ";"
                + "function e(){var s=document.getElementById('__tvcss');"
                + "if(!s){s=document.createElement('style');s.id='__tvcss';"
                + "(document.head||document.documentElement).appendChild(s);}"
                + "if(s.textContent!==window.__tvcss){s.textContent=window.__tvcss;}}"
                + "e();if(!window.__tvcssTimer){window.__tvcssTimer=setInterval(e,1000);}})();";
        pageView.evaluateJavascript(js, null);
    }

    @Override
    protected void onResume() {
        super.onResume();
        hideBars();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideBars();
    }

    @SuppressWarnings("deprecation")
    private void hideBars() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
    }

    /** VOLTAR (ou MENU) na tela de projeção volta para a tela de IP; na tela de IP, fecha o app. */
    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK || keyCode == KeyEvent.KEYCODE_MENU) {
            if (!onSetup) {
                openSetup();
                return true;
            }
            if (keyCode == KeyEvent.KEYCODE_BACK) {
                finish();
                return true;
            }
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.loadUrl("about:blank");
            web.destroy();
        }
        if (pageView != null) {
            pageView.loadUrl("about:blank");
            pageView.destroy();
        }
        super.onDestroy();
    }

    private class Bridge {
        @JavascriptInterface
        public String getIp() {
            return prefs.getString("ip", "");
        }

        @JavascriptInterface
        public void connect(final String ip) {
            if (ip == null) return;
            final String clean = ip.trim();
            if (!HOST.matcher(clean).matches()) return;
            prefs.edit().putString("ip", clean).apply();
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    openTv(clean);
                }
            });
        }

        @JavascriptInterface
        public void showUrl(final String url, final String css) {
            if (url == null || !(url.startsWith("http://") || url.startsWith("https://"))) return;
            final String c = css == null ? "" : css;
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    pageCss = c;
                    pageView.setVisibility(View.VISIBLE);
                    pageView.loadUrl(url);
                    pageView.requestFocus();
                }
            });
        }

        @JavascriptInterface
        public void hideUrl() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    hidePage();
                }
            });
        }

        @JavascriptInterface
        public void setCss(final String css) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    pageCss = css == null ? "" : css;
                    if (pageView.getVisibility() == View.VISIBLE) injectCss();
                }
            });
        }

        @JavascriptInterface
        public void reloadUrl() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    if (pageView.getVisibility() == View.VISIBLE) pageView.reload();
                }
            });
        }
    }
}
