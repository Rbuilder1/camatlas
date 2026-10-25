// CamAtlas for iPhone: a full-screen app around the hosted CamAtlas web app
// (https://rbuilder1.github.io/camatlas/), which GitHub rebuilds every 6 hours with fresh camera lists.
// The page already handles the notch and home bar; this adds what a web page can't do on its own:
// inline live video, Messages / Safari for links it hands off, and a retry screen when offline.
import SwiftUI
import WebKit

let siteURL = URL(string: "https://rbuilder1.github.io/camatlas/")!
let background = UIColor(red: 11 / 255, green: 12 / 255, blue: 15 / 255, alpha: 1)

@main
struct CamAtlasApp: App {
    var body: some Scene {
        WindowGroup {
            CamAtlasView()
                .ignoresSafeArea()
                .background(Color(background))
                .preferredColorScheme(.dark)
        }
    }
}

struct CamAtlasView: UIViewRepresentable {
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true                  // live cameras play in place, not full-screen
        config.mediaTypesRequiringUserActionForPlayback = []     // the Monitor wall starts its streams itself
        config.applicationNameForUserAgent = "CamAtlasApp/1.0"
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        web.isOpaque = false
        web.backgroundColor = background
        web.scrollView.backgroundColor = background
        web.scrollView.contentInsetAdjustmentBehavior = .never  // the page pads for the notch itself
        web.scrollView.bounces = false
        web.load(URLRequest(url: siteURL))
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        // CamAtlas pages, map tiles and camera media load here; anything else the page navigates to
        // (Text a friend's sms: link, Google Maps, an agency's site) opens in the app made for it.
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else { return decisionHandler(.allow) }
            let inFrame = action.targetFrame.map { !$0.isMainFrame } ?? false  // e.g. the Street View iframe
            if url.host == siteURL.host || inFrame || ["about", "blob", "data"].contains(url.scheme ?? "") {
                return decisionHandler(.allow)
            }
            UIApplication.shared.open(url)
            decisionHandler(.cancel)
        }

        // Links that ask for a new window (target="_blank") open in Safari.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url { UIApplication.shared.open(url) }
            return nil
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            showOffline(webView)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            if (error as NSError).code != NSURLErrorCancelled { showOffline(webView) }
        }

        private func showOffline(_ webView: WKWebView) {
            let page = """
            <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
            <body style="margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
            background:#0b0c0f;color:#e8eaee;font:17px -apple-system,sans-serif;text-align:center;padding:24px;box-sizing:border-box">
            <p style="font-size:22px;font-weight:600;margin:0 0 8px">CamAtlas can't reach the internet</p>
            <p style="color:#8b919c;margin:0 0 24px">The camera map needs a connection. Check Wi-Fi or mobile data.</p>
            <a href="\(siteURL.absoluteString)" style="background:#f2f3f5;color:#111;padding:12px 28px;border-radius:10px;
            text-decoration:none;font-weight:600">Try again</a></body></html>
            """
            webView.loadHTMLString(page, baseURL: siteURL)
        }
    }
}
