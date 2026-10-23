package com.natantec.tvcast;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.text.Spannable;
import android.text.SpannableString;
import android.text.style.ForegroundColorSpan;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;

import org.json.JSONObject;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.SocketTimeoutException;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.List;
import java.util.regex.Pattern;

/**
 * TV Cast (TV) 1.2
 * - WebView em tela cheia que abre http://IP:8765/tv (controlada pelo PC).
 * - Acha o PC sozinho na rede (broadcast UDP) e reencontra se o IP mudar.
 * - Selo nativo "TV CAST + status" por cima de tudo (inclusive de páginas da web).
 * - Site (URL) numa segunda WebView criada só quando preciso (economiza memória), com CSS injetado.
 */
public class MainActivity extends Activity {

    private static final String SETUP_URL = "file:///android_asset/setup.html";
    private static final Pattern HOST = Pattern.compile("^[A-Za-z0-9.\\-]+(:\\d{1,5})?$");
    private static final int DISCOVERY_PORT = 8766;

    private FrameLayout root;
    private WebView web;          // página do TV Cast (controlada pelo PC)
    private WebView pageView;     // site mostrado por cima (sem a ponte Android); criada sob demanda
    private TextView badge;
    private String pageCss = "";
    private SharedPreferences prefs;
    private boolean onSetup = true;
    private volatile boolean discovering = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        prefs = getSharedPreferences("tvcast", MODE_PRIVATE);

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        badge = new TextView(this);
        badge.setTextColor(Color.WHITE);
        badge.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        badge.setPadding(dp(18), dp(7), dp(18), dp(7));
        GradientDrawable bgd = new GradientDrawable();
        bgd.setColor(0xB8000000);
        bgd.setCornerRadius(dp(20));
        badge.setBackground(bgd);
        badge.setVisibility(View.GONE);
        FrameLayout.LayoutParams blp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
        blp.bottomMargin = dp(22);
        root.addView(badge, blp);
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
                    setBadge(false, "", false);
                    v.loadUrl(SETUP_URL + "?err=1");
                }
            }

            // TV com pouca RAM: se o processo do navegador morrer, reabre o app em vez de travar.
            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        recreate();
                    }
                });
                return true;
            }
        });

        String ip = prefs.getString("ip", "");
        if (ip.length() > 0) {
            openTv(ip);
        } else {
            openSetup("");
        }
    }

    private int dp(float v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                getResources().getDisplayMetrics());
    }

    private void baseSettings(WebView w) {
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
    }

    private void openSetup(String query) {
        onSetup = true;
        hidePage();
        setBadge(false, "", false);
        web.loadUrl(SETUP_URL + query);
    }

    private void openTv(String hostPort) {
        onSetup = false;
        String h = hostPort.contains(":") ? hostPort : hostPort + ":8765";
        web.loadUrl("http://" + h + "/tv");
    }

    // ------------------------------------------------------------ selo TV CAST + status
    private void setBadge(boolean show, String text, boolean ok) {
        if (!show) {
            badge.setVisibility(View.GONE);
            return;
        }
        SpannableString sp = new SpannableString("\u25CF  " + text);
        sp.setSpan(new ForegroundColorSpan(ok ? 0xFF33C27F : 0xFFE5534B), 0, 1,
                Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        badge.setText(sp);
        badge.setVisibility(View.VISIBLE);
    }

    // ------------------------------------------------------------ página da web por cima
    private WebView ensurePageView() {
        if (pageView != null) return pageView;
        WebView w = new WebView(this);
        w.setBackgroundColor(Color.BLACK);
        baseSettings(w);
        w.setWebChromeClient(new WebChromeClient());
        w.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView v, String url) {
                injectCss();
            }

            @Override
            public void onPageCommitVisible(WebView v, String url) {
                injectCss();
            }

            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        recreate();
                    }
                });
                return true;
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
        root.addView(w, 1, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        pageView = w;
        return w;
    }

    private void hidePage() {
        if (pageView == null) return;
        WebView w = pageView;
        pageView = null;
        root.removeView(w);
        w.stopLoading();
        w.destroy();
    }

    /** Mantém o CSS personalizado no site (e reaplica se o site recriar o head). */
    private void injectCss() {
        if (pageView == null) return;
        String q = JSONObject.quote(pageCss).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029");
        String js = "(function(){window.__tvcss=" + q + ";"
                + "function e(){var s=document.getElementById('__tvcss');"
                + "if(!s){s=document.createElement('style');s.id='__tvcss';"
                + "(document.head||document.documentElement).appendChild(s);}"
                + "if(s.textContent!==window.__tvcss){s.textContent=window.__tvcss;}}"
                + "e();if(!window.__tvcssTimer){window.__tvcssTimer=setInterval(e,1000);}})();";
        pageView.evaluateJavascript(js, null);
    }

    // ------------------------------------------------------------ busca automática do PC
    private void discover() {
        if (discovering) return;
        discovering = true;
        new Thread(new Runnable() {
            @Override
            public void run() {
                String found = "";
                try {
                    found = doDiscover();
                } catch (Exception ignored) {
                }
                discovering = false;
                final String res = found;
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        if (onSetup && web != null) {
                            web.evaluateJavascript("window.onDiscover&&window.onDiscover("
                                    + JSONObject.quote(res) + ")", null);
                        }
                    }
                });
            }
        }).start();
    }

    private String doDiscover() throws Exception {
        List<InetAddress> targets = new ArrayList<InetAddress>();
        targets.add(InetAddress.getByName("255.255.255.255"));
        Enumeration<NetworkInterface> nis = NetworkInterface.getNetworkInterfaces();
        while (nis != null && nis.hasMoreElements()) {
            NetworkInterface ni = nis.nextElement();
            if (!ni.isUp() || ni.isLoopback()) continue;
            for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                InetAddress b = ia.getBroadcast();
                if (b != null) targets.add(b);
            }
        }

        byte[] req = "TVCAST?".getBytes("UTF-8");
        byte[] buf = new byte[512];
        DatagramSocket sock = new DatagramSocket();
        try {
            sock.setBroadcast(true);
            sock.setSoTimeout(400);
            long end = System.currentTimeMillis() + 4000;
            long nextSend = 0;
            while (System.currentTimeMillis() < end) {
                long now = System.currentTimeMillis();
                if (now >= nextSend) {
                    for (InetAddress t : targets) {
                        try {
                            sock.send(new DatagramPacket(req, req.length, t, DISCOVERY_PORT));
                        } catch (Exception ignored) {
                        }
                    }
                    nextSend = now + 1000;
                }
                try {
                    DatagramPacket p = new DatagramPacket(buf, buf.length);
                    sock.receive(p);
                    JSONObject o = new JSONObject(new String(p.getData(), 0, p.getLength(), "UTF-8"));
                    if ("tvcast".equals(o.optString("app"))) {
                        return p.getAddress().getHostAddress() + ":" + o.optInt("port", 8765);
                    }
                } catch (SocketTimeoutException e) {
                    // continua tentando até acabar o tempo
                } catch (org.json.JSONException e) {
                    // resposta que não é do TV Cast
                }
            }
        } finally {
            sock.close();
        }
        return "";
    }

    // ------------------------------------------------------------ ciclo de vida / teclas
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

    /** VOLTAR (ou MENU) na projeção abre a tela de conexão; na tela de conexão, VOLTAR fecha o app. */
    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK || keyCode == KeyEvent.KEYCODE_MENU) {
            if (!onSetup) {
                openSetup("?back=1");
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
        hidePage();
        super.onDestroy();
    }

    // ------------------------------------------------------------ ponte JavaScript
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
        public void discover() {
            MainActivity.this.discover();
        }

        @JavascriptInterface
        public void setBadge(final boolean show, final String text, final boolean ok) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    if (!onSetup) MainActivity.this.setBadge(show, text == null ? "" : text, ok);
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
                    WebView w = ensurePageView();
                    w.loadUrl(url);
                    w.requestFocus();
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
                    injectCss();
                }
            });
        }

        @JavascriptInterface
        public void reloadUrl() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    if (pageView != null) pageView.reload();
                }
            });
        }
    }
}
