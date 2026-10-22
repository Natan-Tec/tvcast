package com.natantec.tvcast;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.util.regex.Pattern;

/**
 * TV Cast (TV): só uma WebView em tela cheia.
 * Tela de IP -> abre http://IP:8080/tv, que o servidor do PC controla.
 */
public class MainActivity extends Activity {

    private static final String SETUP_URL = "file:///android_asset/setup.html";
    private static final Pattern HOST = Pattern.compile("^[A-Za-z0-9.\\-]+(:\\d{1,5})?$");

    private WebView web;
    private SharedPreferences prefs;
    private boolean onSetup = true;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        prefs = getSharedPreferences("tvcast", MODE_PRIVATE);

        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setAllowFileAccess(true);

        web.addJavascriptInterface(new Bridge(), "Android");
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame() && !onSetup) {
                    onSetup = true;
                    v.loadUrl(SETUP_URL + "?err=1");
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

    private void openSetup() {
        onSetup = true;
        web.loadUrl(SETUP_URL);
    }

    private void openTv(String hostPort) {
        onSetup = false;
        String h = hostPort.contains(":") ? hostPort : hostPort + ":8765";
        web.loadUrl("http://" + h + "/tv");
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
    }
}
