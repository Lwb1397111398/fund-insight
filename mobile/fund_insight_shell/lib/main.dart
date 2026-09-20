import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

/// Fund Insight 手机壳：整站就是 web/index.html 这个单页应用，
/// APK 只负责把它装进手机并保留登录口令（DOM storage 持久化）。
///
/// 换地址不用改代码：
///   flutter build apk --release --dart-define=BASE_URL=http://192.168.1.20:8002
const String kBaseUrl = String.fromEnvironment(
  'BASE_URL',
  defaultValue: 'https://fund-insight.onrender.com',
);

void main() {
  runApp(const FundInsightApp());
}

class FundInsightApp extends StatelessWidget {
  const FundInsightApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Fund Insight',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF1890FF)),
        useMaterial3: true,
      ),
      home: const ShellPage(),
    );
  }
}

class ShellPage extends StatefulWidget {
  const ShellPage({super.key});

  @override
  State<ShellPage> createState() => _ShellPageState();
}

class _ShellPageState extends State<ShellPage> {
  late final WebViewController _controller;
  bool _loading = true;
  bool _failed = false;
  int _progress = 0;

  @override
  void initState() {
    super.initState();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(const Color(0xFFF5F7FA))
      // DOM storage 默认开启，口令存在 localStorage 里，装一次输一次就够
      ..setNavigationDelegate(
        NavigationDelegate(
          onProgress: (p) => setState(() => _progress = p),
          onPageStarted: (_) => setState(() {
            _loading = true;
            _failed = false;
          }),
          onPageFinished: (_) => setState(() => _loading = false),
          onWebResourceError: (e) {
            if (e.isForMainFrame ?? false) {
              setState(() {
                _loading = false;
                _failed = true;
              });
            }
          },
        ),
      )
      ..loadRequest(Uri.parse(kBaseUrl));
  }

  void _reload() {
    setState(() {
      _failed = false;
      _loading = true;
    });
    _controller.reload();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Fund Insight 基金观点追踪'),
        backgroundColor: const Color(0xFF1890FF),
        foregroundColor: Colors.white,
        actions: [
          if (_loading && !_failed)
            SizedBox(
              width: 60,
              child: LinearProgressIndicator(value: _progress / 100.0),
            ),
          IconButton(
            tooltip: '刷新',
            onPressed: _reload,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: _failed ? _errorView() : WebViewWidget(controller: _controller),
    );
  }

  Widget _errorView() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.cloud_off, size: 48, color: Colors.grey),
            const SizedBox(height: 12),
            Text('打不开 $kBaseUrl',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            const Text('手机没网、或线上服务在冷启动（Render 免费版首个请求要等几十秒）。',
                textAlign: TextAlign.center,
                style: TextStyle(color: Colors.grey)),
            const SizedBox(height: 16),
            FilledButton.icon(
              onPressed: _reload,
              icon: const Icon(Icons.refresh),
              label: const Text('重试'),
            ),
          ],
        ),
      ),
    );
  }
}
